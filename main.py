from __future__ import annotations

import asyncio
import base64
import random
import re
import time
from dataclasses import dataclass
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import At, Image, Plain, Reply
from astrbot.api.star import Context, Star

from .meme_client import MemeClientError, MemeConnectionError, MemeRsClient
from .parser import (
    MemeInfo,
    MemeMatch,
    MemeParseError,
    MemeRegistry,
    parse_options,
    parse_texts,
)
from .qq_button import (
    build_help_keyboard,
    send_qq_official_markdown_keyboard,
    send_qq_official_meme_with_plus_one,
)

AT_TEXT_RE = re.compile(r"@\s*(\d{5,15})")
QQ_OFFICIAL_AT_TAG_RE = re.compile(
    r'(?:<qqbot-at-user\s+id="([^"]+)"\s*/?>|<@!?([A-Za-z0-9_-]{5,64})>)'
)
ATTACHMENT_TAG_RE = re.compile(r"<attachmentType=[^>]*>")
AUTO_RETRY_INTERVAL_SEC = 30.0


@dataclass
class _ImageSource:
    name: str
    data: bytes | None = None
    url: str | None = None


@dataclass
class _MentionTarget:
    user_id: str
    name: str | None = None
    avatar_url: str | None = None
    is_explicit_qq: bool = False


QQ_OFFICIAL_PLATFORMS = {"qq_official", "qq_official_webhook"}
IGNORED_SYNTHETIC_AT_IDS = {"all", "qq_official", "unknown_selfid"}


class MemeRsPlugin(Star):
    """对接本地或远程 meme-generator-rs HTTP 服务的 AstrBot 表情包制作插件。"""

    def __init__(self, context: Context, config: AstrBotConfig | None = None) -> None:
        super().__init__(context)
        self.config: dict[str, Any] = config if config is not None else {}

        base_url = str(self._cfg("base_url", "http://127.0.0.1:2255")).strip()
        if not base_url:
            base_url = "http://127.0.0.1:2255"
        timeout = float(self._cfg("timeout", 30))
        retry = int(self._cfg("retry", 2))

        self.client = MemeRsClient(base_url=base_url, timeout=timeout, retry=retry)
        self.registry = MemeRegistry()
        self._load_lock = asyncio.Lock()
        self._last_load_attempt: float = 0.0

        self._migrate_blacklist_config()
        self._sync_blacklist_schema()

    def _cfg(self, key: str, default: Any) -> Any:
        val = self.config.get(key, default)
        return default if val is None else val

    @property
    def _enable_meme(self) -> bool:
        return bool(self._cfg("enable_meme", True))

    @property
    def _force_prefix(self) -> bool:
        return bool(self._cfg("force_prefix", False))

    @property
    def _prefixes(self) -> list[str]:
        raw = str(self._cfg("prefixes", "#,/"))
        items = [p.strip() for p in raw.split(",") if p.strip()]
        # 长前缀优先匹配
        return sorted(set(items), key=len, reverse=True)

    @property
    def _enable_shortcuts(self) -> bool:
        return bool(self._cfg("enable_shortcuts", True))

    @property
    def _use_base64_upload(self) -> bool:
        return bool(self._cfg("use_base64_upload", True))

    @property
    def _reply_with_quote(self) -> bool:
        return bool(self._cfg("reply_with_quote", False))

    @property
    def _enable_plus_one_button(self) -> bool:
        return bool(self._cfg("enable_plus_one_button", True))

    @property
    def _plus_one_button_label(self) -> str:
        return str(self._cfg("plus_one_button_label", "+1")).strip() or "+1"

    @property
    def _plus_one_button_style(self) -> int:
        try:
            return int(self._cfg("plus_one_button_style", 0))
        except (TypeError, ValueError):
            return 0

    @property
    def _plus_one_send_mode(self) -> str:
        mode = str(self._cfg("plus_one_send_mode", "auto")).strip().lower()
        return mode if mode in {"auto", "markdown", "media"} else "auto"

    @property
    def _error_reply(self) -> bool:
        return bool(self._cfg("error_reply", True))

    @property
    def _avatar_url_template(self) -> str:
        tpl = str(
            self._cfg(
                "avatar_url_template",
                "https://q1.qlogo.cn/g?b=qq&nk={user_id}&s=640",
            )
        ).strip()
        return tpl or "https://q1.qlogo.cn/g?b=qq&nk={user_id}&s=640"

    @property
    def _qq_official_appid(self) -> str:
        return str(self._cfg("qq_official_appid", "")).strip()

    @property
    def _qq_official_avatar_url_template(self) -> str:
        tpl = str(
            self._cfg(
                "qq_official_avatar_url_template",
                "https://q.qlogo.cn/qqapp/{appid}/{user_id}/640",
            )
        ).strip()
        return tpl or "https://q.qlogo.cn/qqapp/{appid}/{user_id}/640"

    @staticmethod
    def _is_qq_official_event(event: AstrMessageEvent) -> bool:
        platform_name = ""
        if hasattr(event, "get_platform_name"):
            platform_name = str(event.get_platform_name() or "").strip().lower()
        return platform_name in QQ_OFFICIAL_PLATFORMS

    def _resolve_qq_official_appid(self, event: AstrMessageEvent) -> str:
        if self._qq_official_appid:
            return self._qq_official_appid

        # 1. 优先从 event.bot.platform.appid 读取（AstrBot qq_official / qq_official_webhook 适配器）
        bot = getattr(event, "bot", None)
        platform_obj = getattr(bot, "platform", None)
        appid = str(getattr(platform_obj, "appid", "") or "").strip()
        if appid:
            return appid

        # 2. 从 context.platform_manager 中按当前 platform_id 匹配
        pm = getattr(self.context, "platform_manager", None)
        if pm is not None and hasattr(pm, "get_insts"):
            target_pid = (
                str(event.get_platform_id() or "").strip()
                if hasattr(event, "get_platform_id")
                else ""
            )
            try:
                for inst in pm.get_insts():
                    meta = inst.meta() if hasattr(inst, "meta") else None
                    if meta and meta.name in QQ_OFFICIAL_PLATFORMS:
                        inst_appid = str(getattr(inst, "appid", "") or "").strip()
                        if inst_appid and (not target_pid or meta.id == target_pid):
                            return inst_appid
            except Exception:
                pass

        # 3. 回退从 botpy http/token 对象读取
        http_obj = getattr(bot, "http", None)
        for attr_owner in (http_obj, getattr(http_obj, "_token", None)):
            if attr_owner is None:
                continue
            for field_name in ("app_id", "appid"):
                val = str(getattr(attr_owner, field_name, "") or "").strip()
                if val:
                    return val

        return ""

    @staticmethod
    def _extract_raw_author_avatar(event: AstrMessageEvent) -> str | None:
        raw_msg = getattr(getattr(event, "message_obj", None), "raw_message", None)
        author = getattr(raw_msg, "author", None)
        avatar = str(getattr(author, "avatar", "") or "").strip()
        if avatar.startswith(("http://", "https://")):
            return avatar
        return None

    def _migrate_blacklist_config(self) -> None:
        """兼容旧版本以英文逗号分隔的字符串配置，自动迁移为 list[str]。"""
        raw = self.config.get("blacklist_memes")
        if isinstance(raw, str):
            items = [part.strip() for part in raw.split(",") if part.strip()]
            self.config["blacklist_memes"] = items
            save_fn = getattr(self.config, "save_config", None)
            if callable(save_fn):
                try:
                    save_fn()
                except Exception as exc:
                    logger.debug(f"[meme_rs] 迁移 blacklist_memes 配置保存失败: {exc}")

    def _sync_blacklist_schema(self) -> None:
        """将当前已同步的表情列表写入配置 schema，供 WebUI 标签下拉框即时搜索。"""
        schema = getattr(self.config, "schema", None)
        if not isinstance(schema, dict):
            return
        field_meta = schema.get("blacklist_memes")
        if not isinstance(field_meta, dict):
            return

        # 若已加载表情库，将旧配置中可能填写的中文关键词规范化为 key（保序去重）
        raw_saved = self.config.get("blacklist_memes")
        saved_items: list[str] = []
        if isinstance(raw_saved, list):
            normalized: list[str] = []
            changed = False
            for item in raw_saved:
                token = str(item or "").strip()
                if not token:
                    changed = True
                    continue
                resolved = self.registry.resolve_key(token) if self.registry.count > 0 else None
                final_val = resolved or token
                if final_val != token:
                    changed = True
                if final_val not in normalized:
                    normalized.append(final_val)
                else:
                    changed = True
            saved_items = normalized
            if changed:
                self.config["blacklist_memes"] = normalized
                save_fn = getattr(self.config, "save_config", None)
                if callable(save_fn):
                    try:
                        save_fn()
                    except Exception:
                        pass

        options: list[str] = []
        labels: list[str] = []
        subtitles: list[str] = []
        seen_keys: set[str] = set()

        def _sort_key(info: MemeInfo) -> tuple[str, str]:
            primary = info.keywords[0] if info.keywords else info.key
            return (primary.lower(), info.key.lower())

        for info in sorted(self.registry.memes_by_key.values(), key=_sort_key):
            seen_keys.add(info.key)
            options.append(info.key)

            # 标题中包含全部关键词与 key，方便在 WebUI 输入任意一个字即时匹配
            kw_list = list(dict.fromkeys(k.strip() for k in info.keywords if k.strip()))
            if kw_list:
                labels.append(f"{' / '.join(kw_list)} ({info.key})")
            else:
                labels.append(info.key)

            img_desc = (
                f"{info.min_images}"
                if info.min_images == info.max_images
                else f"{info.min_images}~{info.max_images}"
            )
            txt_desc = (
                f"{info.min_texts}"
                if info.min_texts == info.max_texts
                else f"{info.min_texts}~{info.max_texts}"
            )
            sub_parts = [f"图 {img_desc} | 文 {txt_desc}"]
            if info.tags:
                sub_parts.append("标签: " + ", ".join(info.tags[:4]))
            subtitles.append(" · ".join(sub_parts))

        # 保留已保存但暂未在服务端列表中的条目，避免断连时标签消失
        for saved in saved_items:
            if saved not in seen_keys:
                seen_keys.add(saved)
                options.append(saved)
                labels.append(saved)
                subtitles.append("已保存（待同步表情库）")

        field_meta["options"] = options
        field_meta["labels"] = labels
        field_meta["subtitles"] = subtitles

    def _get_blacklist_keys(self) -> set[str]:
        raw = self._cfg("blacklist_memes", [])
        tokens: list[str] = []
        if isinstance(raw, list):
            tokens = [str(item).strip() for item in raw if str(item).strip()]
        elif isinstance(raw, str):
            tokens = [part.strip() for part in raw.split(",") if part.strip()]
        if not tokens:
            return set()
        result: set[str] = set()
        for token in tokens:
            resolved = self.registry.resolve_key(token)
            result.add(resolved or token)
        return result

    async def initialize(self) -> None:
        try:
            count = await self._reload_registry(force=True)
            logger.info(
                f"[meme_rs] 已连接 {self.client.base_url}，成功加载 {count} 个表情。"
            )
        except Exception as exc:
            logger.warning(
                f"[meme_rs] 启动时未能连接 {self.client.base_url}（{exc}），"
                "待 meme-generator-rs 服务启动后将自动重连，也可使用 /meme刷新 手动同步。"
            )

    async def terminate(self) -> None:
        await self.client.close()

    async def _reload_registry(self, *, force: bool = False) -> int:
        async with self._load_lock:
            now = time.monotonic()
            if not force and self.registry.count > 0:
                return self.registry.count
            if (
                not force
                and self._last_load_attempt > 0
                and (now - self._last_load_attempt) < AUTO_RETRY_INTERVAL_SEC
            ):
                return self.registry.count
            self._last_load_attempt = now
            raw_infos = await self.client.fetch_infos()
            count = self.registry.load(raw_infos, enable_shortcuts=self._enable_shortcuts)
            self._sync_blacklist_schema()
            return count

    async def _ensure_loaded(self, *, force_on_empty: bool = False) -> bool:
        if self.registry.count > 0:
            return True
        try:
            await self._reload_registry(force=force_on_empty)
            return self.registry.count > 0
        except Exception as exc:
            logger.debug(f"[meme_rs] 自动拉取表情列表失败: {exc}")
            return False

    # ------------------------------------------------------------------
    # 消息组件解析与图片/头像提取
    # ------------------------------------------------------------------

    @staticmethod
    def _get_chain(event: AstrMessageEvent) -> list[Any]:
        if hasattr(event, "get_messages"):
            msgs = event.get_messages()
            if isinstance(msgs, list):
                return msgs
        msg_obj = getattr(event, "message_obj", None)
        chain = getattr(msg_obj, "message", None)
        return list(chain) if isinstance(chain, list) else []

    def _extract_raw_text_and_mentions(
        self, event: AstrMessageEvent
    ) -> tuple[str, list[_MentionTarget]]:
        """从原始消息链中提取纯文本与 @提及列表（跳过开头的唤醒 @Bot 与官 Bot 合成 At）。"""
        chain = self._get_chain(event)
        self_id = str(event.get_self_id() or "")
        plain_parts: list[str] = []
        mentions: list[_MentionTarget] = []
        seen_mention_ids: set[str] = set()
        seen_non_empty_text = False

        for seg in chain:
            if isinstance(seg, Plain):
                txt = ATTACHMENT_TAG_RE.sub("", seg.text or "")
                # 解析 QQ 官 Bot 文本中内嵌的 <@OPENID> / <@!OPENID> / <qqbot-at-user id="..."/>
                for m in QQ_OFFICIAL_AT_TAG_RE.finditer(txt):
                    uid = str(m.group(1) or m.group(2) or "").strip()
                    if not uid or uid.lower() in IGNORED_SYNTHETIC_AT_IDS:
                        continue
                    prefix_before = QQ_OFFICIAL_AT_TAG_RE.sub("", txt[: m.start()]).strip()
                    has_text_before = seen_non_empty_text or bool(prefix_before)
                    if not has_text_before and self_id and uid == self_id:
                        continue
                    if uid not in seen_mention_ids:
                        seen_mention_ids.add(uid)
                        mentions.append(_MentionTarget(user_id=uid))
                txt = QQ_OFFICIAL_AT_TAG_RE.sub(" ", txt)
                if txt.strip():
                    seen_non_empty_text = True
                if txt:
                    plain_parts.append(txt)
            elif isinstance(seg, At):
                qq = str(getattr(seg, "qq", "") or "").strip()
                if not qq or qq.lower() in IGNORED_SYNTHETIC_AT_IDS:
                    continue
                # 位于消息文本之前且指向机器人自身的 @ 视为唤醒词，不当作表情目标头像
                if not seen_non_empty_text and self_id and qq == self_id:
                    continue
                if qq not in seen_mention_ids:
                    seen_mention_ids.add(qq)
                    name = getattr(seg, "name", None)
                    mentions.append(
                        _MentionTarget(
                            user_id=qq,
                            name=str(name).strip() if name else None,
                        )
                    )

        # QQ 官 Bot：如果原始消息包含其他非机器人 @提及（频道或白名单群），补充或完善其 openid 与 avatar
        if self._is_qq_official_event(event):
            raw_msg = getattr(getattr(event, "message_obj", None), "raw_message", None)
            raw_mentions = getattr(raw_msg, "mentions", None) or []
            mentions_by_id = {m.user_id: m for m in mentions}
            for m in raw_mentions:
                if getattr(m, "is_you", False) is True or getattr(m, "bot", False) is True:
                    continue
                uid = str(
                    getattr(m, "member_openid", None)
                    or getattr(m, "user_openid", None)
                    or getattr(m, "id", None)
                    or ""
                ).strip()
                if not uid or uid == self_id:
                    continue
                uname = str(getattr(m, "username", "") or "").strip() or None
                avatar = str(getattr(m, "avatar", "") or "").strip()
                avatar_url = avatar if avatar.startswith(("http://", "https://")) else None
                existing = mentions_by_id.get(uid)
                if existing is not None:
                    if not existing.name and uname:
                        existing.name = uname
                    if not existing.avatar_url and avatar_url:
                        existing.avatar_url = avatar_url
                else:
                    seen_mention_ids.add(uid)
                    target = _MentionTarget(user_id=uid, name=uname, avatar_url=avatar_url)
                    mentions.append(target)
                    mentions_by_id[uid] = target

        raw_text = " ".join(plain_parts).strip()
        if not raw_text and not plain_parts:
            fallback = ATTACHMENT_TAG_RE.sub("", event.get_message_str() or "")
            raw_text = QQ_OFFICIAL_AT_TAG_RE.sub(" ", fallback).strip()
        return raw_text, mentions

    async def _extract_reply_info(
        self, event: AstrMessageEvent
    ) -> tuple[str | None, str | None, list[_ImageSource]]:
        """提取引用消息中的发送者 ID、昵称与图片列表。"""
        chain = self._get_chain(event)
        reply_seg = next((s for s in chain if isinstance(s, Reply)), None)
        if reply_seg is None:
            return None, None, []

        quoted_uid: str | None = None
        for candidate in (getattr(reply_seg, "sender_id", None), getattr(reply_seg, "qq", None)):
            if candidate is not None:
                s = str(candidate).strip()
                if s and s != "0":
                    quoted_uid = s
                    break

        quoted_name = str(getattr(reply_seg, "sender_nickname", "") or "").strip() or None
        quoted_images: list[_ImageSource] = []

        reply_chain = getattr(reply_seg, "chain", None) or []
        for seg in reply_chain:
            if isinstance(seg, Image):
                src = await self._image_segment_to_source(
                    seg, name=quoted_name or quoted_uid or ""
                )
                if src is not None:
                    quoted_images.append(src)

        # 若适配器未填充 Reply.chain 或缺少引用者信息，尝试通过 OneBot get_msg 回退查询
        if (not quoted_images and not quoted_uid) or (
            not reply_chain and getattr(reply_seg, "id", None)
        ):
            bot = getattr(event, "bot", None)
            api = getattr(bot, "api", None)
            reply_id = getattr(reply_seg, "id", None)
            if api is not None and reply_id is not None:
                try:
                    msg_id_param: Any = int(reply_id) if str(reply_id).isdigit() else reply_id
                    res = await api.call_action("get_msg", message_id=msg_id_param)
                    if isinstance(res, dict):
                        sender = res.get("sender") or {}
                        if not quoted_uid and sender.get("user_id"):
                            quoted_uid = str(sender["user_id"])
                        if not quoted_name:
                            quoted_name = (
                                str(sender.get("card") or sender.get("nickname") or "").strip()
                                or None
                            )
                        raw_msg = res.get("message")
                        if not quoted_images and isinstance(raw_msg, list):
                            for elem in raw_msg:
                                if not isinstance(elem, dict) or elem.get("type") != "image":
                                    continue
                                data_dict = elem.get("data") or {}
                                img_url = data_dict.get("url") or data_dict.get("file")
                                if isinstance(img_url, str) and img_url.strip():
                                    src = await self._raw_ref_to_source(
                                        img_url.strip(),
                                        name=quoted_name or quoted_uid or "",
                                    )
                                    if src is not None:
                                        quoted_images.append(src)
                except Exception as exc:
                    logger.debug(f"[meme_rs] OneBot get_msg 回退失败: {exc}")

        return quoted_uid, quoted_name, quoted_images

    async def _image_segment_to_source(
        self, seg: Image, name: str = ""
    ) -> _ImageSource | None:
        url = (getattr(seg, "url", None) or "").strip()
        file_val = (getattr(seg, "file", None) or "").strip()

        if not self._use_base64_upload:
            for cand in (url, file_val):
                if cand.startswith(("http://", "https://")):
                    return _ImageSource(name=name, url=cand)

        try:
            b64 = await seg.convert_to_base64()
            if b64:
                return _ImageSource(name=name, data=base64.b64decode(b64))
        except Exception as exc:
            logger.debug(f"[meme_rs] Image.convert_to_base64 失败，尝试回退: {exc}")

        for cand in (url, file_val):
            if cand:
                src = await self._raw_ref_to_source(cand, name=name)
                if src is not None:
                    return src
        return None

    async def _raw_ref_to_source(self, ref: str, name: str = "") -> _ImageSource | None:
        if ref.startswith(("http://", "https://")):
            if not self._use_base64_upload:
                return _ImageSource(name=name, url=ref)
            try:
                data = await self.client.download_image_bytes(ref)
                return _ImageSource(name=name, data=data)
            except Exception as exc:
                logger.warning(f"[meme_rs] 下载图片失败: {exc}")
                return None
        if ref.startswith("base64://"):
            try:
                return _ImageSource(
                    name=name, data=base64.b64decode(ref[len("base64://") :])
                )
            except Exception:
                return None
        return None

    async def _resolve_user_name(
        self,
        event: AstrMessageEvent,
        user_id: str,
        hint_name: str | None = None,
    ) -> str:
        if hint_name and hint_name.strip():
            return hint_name.strip()
        sender_id = str(event.get_sender_id() or "")
        if user_id == sender_id:
            sender_name = str(event.get_sender_name() or "").strip()
            if sender_name:
                return sender_name

        bot = getattr(event, "bot", None)
        api = getattr(bot, "api", None)
        group_id = str(event.get_group_id() or "").strip()
        if api is not None and user_id.isdigit():
            try:
                if group_id and group_id.isdigit():
                    info = await api.call_action(
                        "get_group_member_info",
                        group_id=int(group_id),
                        user_id=int(user_id),
                    )
                    if isinstance(info, dict):
                        nick = str(info.get("card") or info.get("nickname") or "").strip()
                        if nick:
                            return nick
                else:
                    info = await api.call_action(
                        "get_stranger_info",
                        user_id=int(user_id),
                    )
                    if isinstance(info, dict):
                        nick = str(info.get("nickname") or "").strip()
                        if nick:
                            return nick
            except Exception:
                pass
        return user_id

    def _build_avatar_candidate_urls(
        self,
        event: AstrMessageEvent,
        user_id: str,
        *,
        hint_avatar_url: str | None = None,
        is_explicit_qq: bool = False,
    ) -> list[str]:
        candidates: list[str] = []

        def _add(url: str | None) -> None:
            if url and url not in candidates:
                candidates.append(url)

        # 1. 优先使用平台直接下发的头像 URL（如 QQ 频道 author.avatar / mentions[].avatar）
        if hint_avatar_url and hint_avatar_url.startswith(("http://", "https://")):
            _add(hint_avatar_url)

        sender_id = str(event.get_sender_id() or "").strip()
        if user_id == sender_id:
            _add(self._extract_raw_author_avatar(event))

        # 2. QQ 官 Bot 群聊/私聊 OpenID 头像规则（手动输入 @QQ号 时仍走普通 QQ 号模板）
        is_numeric_qq = user_id.isdigit() and 5 <= len(user_id) <= 12
        if self._is_qq_official_event(event) and not is_explicit_qq and not is_numeric_qq:
            appid = self._resolve_qq_official_appid(event)
            if not appid and not candidates:
                raise MemeClientError(
                    "未能自动获取 QQ 官 Bot AppID，请在插件配置中填写 qq_official_appid。"
                )
            if appid:
                primary = self._qq_official_avatar_url_template.format(
                    appid=appid,
                    user_id=user_id,
                    openid=user_id,
                )
                _add(primary)
                _add(f"https://thirdqq.qlogo.cn/qqapp/{appid}/{user_id}/640")
                _add(f"https://q.qlogo.cn/qqapp/{appid}/{user_id}/100")
                _add(f"https://thirdqq.qlogo.cn/qqapp/{appid}/{user_id}/100")
            return candidates

        # 3. OneBot / 手动 @QQ号 场景
        _add(self._avatar_url_template.format(user_id=user_id))
        return candidates

    async def _fetch_avatar_source(
        self,
        event: AstrMessageEvent,
        user_id: str,
        hint_name: str | None = None,
        hint_avatar_url: str | None = None,
        is_explicit_qq: bool = False,
    ) -> _ImageSource:
        candidate_urls = self._build_avatar_candidate_urls(
            event,
            user_id,
            hint_avatar_url=hint_avatar_url,
            is_explicit_qq=is_explicit_qq,
        )
        name = await self._resolve_user_name(event, user_id, hint_name=hint_name)
        if not self._use_base64_upload:
            return _ImageSource(name=name, url=candidate_urls[0])

        last_exc: Exception | None = None
        for url in candidate_urls:
            try:
                data = await self.client.download_image_bytes(url)
                return _ImageSource(name=name, data=data)
            except Exception as exc:
                last_exc = exc
                logger.debug(f"[meme_rs] 拉取头像失败 ({url}): {exc}")
        raise MemeClientError(f"获取用户头像失败: {last_exc}")

    async def _collect_images_for_meme(
        self,
        event: AstrMessageEvent,
        meme: MemeInfo,
        mentions: list[_MentionTarget],
        preset_names: list[str] | None = None,
    ) -> list[dict[str, str]]:
        if meme.min_images == 0 and meme.max_images == 0:
            return []

        sender_id = str(event.get_sender_id() or "").strip()
        sender_name = str(event.get_sender_name() or sender_id).strip()

        quoted_uid, quoted_name, quoted_images = await self._extract_reply_info(event)

        # 提取当前消息附带的图片
        current_images: list[_ImageSource] = []
        for seg in self._get_chain(event):
            if isinstance(seg, Image):
                src = await self._image_segment_to_source(seg, name=sender_name)
                if src is not None:
                    current_images.append(src)

        sources: list[_ImageSource] = []
        # 1. 引用消息图片 + 当前消息图片
        sources.extend(quoted_images)
        sources.extend(current_images)

        # 2. @提及的用户头像（去重保序）
        used_user_ids: set[str] = set()
        mention_sources: list[_ImageSource] = []
        for target in mentions:
            if target.user_id in used_user_ids:
                continue
            used_user_ids.add(target.user_id)
            mention_sources.append(
                await self._fetch_avatar_source(
                    event,
                    target.user_id,
                    hint_name=target.name,
                    hint_avatar_url=target.avatar_url,
                    is_explicit_qq=target.is_explicit_qq,
                )
            )

        # 3. 若没有直接图片且引用了某人的纯文本消息，则加入被引用人的头像
        if not sources and quoted_uid and quoted_uid not in used_user_ids:
            used_user_ids.add(quoted_uid)
            mention_sources.append(
                await self._fetch_avatar_source(event, quoted_uid, hint_name=quoted_name)
            )

        # 优先放 @/引用头像，再放消息附带图片（若用户直接发图且无 @，则直接用附带图片）
        # 如果用户既没 @ 也没引用纯文本，sources 里就是消息图；
        # 如果用户同时 @某人 并带图，头像在前、附带图在后。
        combined = mention_sources + sources

        # 4. 图片数量不足 min_images 时，用发送者头像自动补齐
        if len(combined) < meme.min_images and sender_id:
            sender_avatar = await self._fetch_avatar_source(
                event, sender_id, hint_name=sender_name
            )
            if len(combined) > 0 and meme.min_images >= 2:
                # 双人表情（如 亲 @对方）：发送者放第一位，对方放第二位
                combined.insert(0, sender_avatar)
            else:
                combined.append(sender_avatar)

        if meme.max_images > 0:
            combined = combined[: meme.max_images]

        if len(combined) < meme.min_images:
            if meme.min_images == meme.max_images:
                raise MemeParseError(f"该表情需要 {meme.min_images} 张图片")
            raise MemeParseError(
                f"该表情需要 {meme.min_images} ~ {meme.max_images} 张图片"
            )

        # 若 shortcut 指定了固定 names，则覆盖图片名称
        if preset_names:
            for idx, custom_name in enumerate(preset_names):
                if idx < len(combined) and custom_name:
                    combined[idx].name = custom_name

        # 上传所有图片到 meme-generator-rs 服务端
        async def _upload(src: _ImageSource) -> dict[str, str]:
            if src.data is not None:
                image_id = await self.client.upload_image_bytes(src.data)
            elif src.url is not None:
                image_id = await self.client.upload_image_url(src.url)
            else:
                raise MemeParseError("读取图片数据失败")
            return {"name": src.name or "用户", "id": image_id}

        return list(await asyncio.gather(*(_upload(src) for src in combined)))

    def _build_image_result(
        self,
        event: AstrMessageEvent,
        img_bytes: bytes,
        caption: str | None = None,
    ):
        chain: list[Any] = []
        if self._reply_with_quote:
            msg_id = getattr(getattr(event, "message_obj", None), "message_id", None)
            if msg_id:
                chain.append(Reply(id=msg_id))
        if caption:
            chain.append(Plain(caption))
        chain.append(Image.fromBytes(img_bytes))
        return event.chain_result(chain)

    def _format_plus_one_command(self, used_prefix: str, trigger: str) -> str:
        prefix = used_prefix
        if not prefix and self._force_prefix and self._prefixes:
            prefix = self._prefixes[0]
        return f"{prefix}{trigger} "

    async def _deliver_meme_image(
        self,
        event: AstrMessageEvent,
        img_bytes: bytes,
        *,
        trigger_command: str | None = None,
        caption: str | None = None,
    ):
        """发送生成的表情包；在 QQ 官 Bot 上启用 +1 按钮时，同消息附带图片与 +1 按钮。"""
        if (
            trigger_command
            and self._enable_plus_one_button
            and self._is_qq_official_event(event)
        ):
            sent = await send_qq_official_meme_with_plus_one(
                event,
                img_bytes,
                trigger_command,
                caption=caption,
                label=self._plus_one_button_label,
                style=self._plus_one_button_style,
                mode=self._plus_one_send_mode,
                reply_with_quote=self._reply_with_quote,
                log=logger,
            )
            if sent:
                return
        yield self._build_image_result(event, img_bytes, caption=caption)

    async def _execute_meme(
        self,
        event: AstrMessageEvent,
        match: MemeMatch,
        mentions: list[_MentionTarget],
    ) -> bytes:
        meme = match.meme

        # 从文本中提取手写 @QQ号（例如 hello @10001）
        text_at_ids = AT_TEXT_RE.findall(match.remaining_text)
        cleaned_remaining = AT_TEXT_RE.sub("", match.remaining_text).strip()
        all_mentions = list(mentions) + [
            _MentionTarget(user_id=uid, is_explicit_qq=True) for uid in text_at_ids
        ]

        # 1. 解析选项 (#opt val / --opt val)
        options, text_without_opts = parse_options(
            meme, cleaned_remaining, preset_options=match.preset_options
        )

        # 2. 解析文本
        texts = parse_texts(
            meme, text_without_opts, preset_texts=match.preset_texts
        )

        # 3. 收集并上传图片
        images = await self._collect_images_for_meme(
            event, meme, all_mentions, preset_names=match.preset_names
        )

        # 4. 调用 meme-generator-rs 生成表情
        return await self.client.generate_meme(meme.key, images, texts, options)

    # ------------------------------------------------------------------
    # 管理与查询辅助方法
    # ------------------------------------------------------------------

    async def _handle_list(self, event: AstrMessageEvent):
        if not await self._ensure_loaded(force_on_empty=True):
            yield event.plain_result(
                f"无法连接 meme-generator-rs 服务 ({self.client.base_url})，请确认服务端已运行且地址配置正确。"
            )
            event.stop_event()
            return
        try:
            img_bytes = await self.client.render_list()
            yield self._build_image_result(event, img_bytes)
        except Exception as exc:
            logger.error(f"[meme_rs] 渲染表情列表失败: {exc}")
            yield event.plain_result(f"获取表情列表失败: {exc}")
        event.stop_event()

    async def _handle_info(self, event: AstrMessageEvent, query: str):
        query = query.strip()
        if not query:
            yield event.plain_result("用法：/meme详情 <表情关键词或key>，例如：/meme详情 hello")
            event.stop_event()
            return
        if not await self._ensure_loaded(force_on_empty=True):
            yield event.plain_result(
                f"无法连接 meme-generator-rs 服务 ({self.client.base_url})，请确认服务端已运行且地址配置正确。"
            )
            event.stop_event()
            return

        meme = self.registry.get_info(query)
        if meme is None:
            candidates = self.registry.search_local(query)[:8]
            if candidates:
                tips = "、".join(
                    (c.keywords[0] if c.keywords else c.key) for c in candidates
                )
                yield event.plain_result(
                    f"未找到表情「{query}」，你可能在找：{tips}"
                )
            else:
                yield event.plain_result(f"未找到表情「{query}」的信息。")
            event.stop_event()
            return

        img_desc = (
            str(meme.min_images)
            if meme.min_images == meme.max_images
            else f"{meme.min_images} ~ {meme.max_images}"
        )
        txt_desc = (
            str(meme.min_texts)
            if meme.min_texts == meme.max_texts
            else f"{meme.min_texts} ~ {meme.max_texts}"
        )
        lines = [
            f"名称(key): {meme.key}",
            f"关键词: {' / '.join(meme.keywords) if meme.keywords else '无'}",
            f"图片数量: {img_desc}",
            f"文本数量: {txt_desc}",
        ]
        if meme.default_texts:
            lines.append(f"默认文本: {' / '.join(meme.default_texts)}")
        if meme.tags:
            lines.append(f"标签: {', '.join(meme.tags)}")
        if meme.presets:
            lines.append(f"快捷触发: {' / '.join(meme.presets[:12])}")
        if meme.options:
            opt_lines = []
            for opt in meme.options:
                name = opt.get("name", "")
                otype = opt.get("type", "")
                desc = opt.get("description") or ""
                extra_parts = []
                if opt.get("choices"):
                    extra_parts.append(f"可选: {'/'.join(map(str, opt['choices']))}")
                if opt.get("minimum") is not None or opt.get("maximum") is not None:
                    extra_parts.append(
                        f"范围: {opt.get('minimum', '-∞')}~{opt.get('maximum', '+∞')}"
                    )
                if opt.get("default") is not None:
                    extra_parts.append(f"默认: {opt['default']}")
                detail = "，".join(p for p in [desc, *extra_parts] if p)
                opt_lines.append(f"  #{name} ({otype})" + (f" - {detail}" if detail else ""))
            lines.append("可选参数:\n" + "\n".join(opt_lines))

        info_text = "\n".join(lines)
        try:
            preview_bytes = await self.client.fetch_preview(meme.key)
            yield self._build_image_result(
                event, preview_bytes, caption=info_text + "\n预览图:\n"
            )
        except Exception as exc:
            logger.warning(f"[meme_rs] 获取预览图失败 ({meme.key}): {exc}")
            yield event.plain_result(info_text + "\n（预览图获取失败）")
        event.stop_event()

    async def _handle_search(self, event: AstrMessageEvent, query: str):
        query = query.strip()
        if not query:
            yield event.plain_result("用法：/meme搜索 <关键词>，例如：/meme搜索 摸")
            event.stop_event()
            return
        if not await self._ensure_loaded(force_on_empty=True):
            yield event.plain_result(
                f"无法连接 meme-generator-rs 服务 ({self.client.base_url})，请确认服务端已运行且地址配置正确。"
            )
            event.stop_event()
            return

        results: list[MemeInfo] = []
        seen: set[str] = set()

        # 合并服务端搜索与本地搜索
        try:
            server_keys = await self.client.search(query)
            for key in server_keys:
                info = self.registry.memes_by_key.get(key)
                if info and info.key not in seen:
                    seen.add(info.key)
                    results.append(info)
        except Exception:
            pass

        for info in self.registry.search_local(query):
            if info.key not in seen:
                seen.add(info.key)
                results.append(info)

        if not results:
            yield event.plain_result(f"没有找到与「{query}」相关的表情。")
            event.stop_event()
            return

        max_show = 25
        lines = [f"找到 {len(results)} 个与「{query}」相关的表情："]
        for idx, info in enumerate(results[:max_show], start=1):
            kws = " / ".join(info.keywords) if info.keywords else info.key
            lines.append(f"{idx}. {kws} ({info.key})")
        if len(results) > max_show:
            lines.append(f"... 仅展示前 {max_show} 个，请使用更精确的关键词搜索")
        yield event.plain_result("\n".join(lines))
        event.stop_event()

    async def _handle_random(self, event: AstrMessageEvent):
        if not await self._ensure_loaded(force_on_empty=True):
            yield event.plain_result(
                f"无法连接 meme-generator-rs 服务 ({self.client.base_url})，请确认服务端已运行且地址配置正确。"
            )
            event.stop_event()
            return

        blacklist = self._get_blacklist_keys()
        candidates = [
            m
            for m in self.registry.memes_by_key.values()
            if m.key not in blacklist
            and m.min_images <= 1
            and (m.min_texts == 0 or len(m.default_texts) >= m.min_texts)
            and (m.min_images >= 1 or m.min_texts >= 1)
        ]
        if not candidates:
            yield event.plain_result("当前没有可用于随机生成的表情。")
            event.stop_event()
            return

        random.shuffle(candidates)
        raw_text, mentions = self._extract_raw_text_and_mentions(event)
        used_prefix = ""
        for prefix in self._prefixes:
            if raw_text.startswith(prefix):
                used_prefix = prefix
                break
        if not used_prefix and self._is_qq_official_event(event):
            used_prefix = "/"
        last_err: Exception | None = None

        for meme in candidates[:5]:
            match = MemeMatch(
                meme=meme,
                matched_trigger=meme.keywords[0] if meme.keywords else meme.key,
                remaining_text="",
            )
            try:
                img_bytes = await self._execute_meme(event, match, mentions)
                aliases = " / ".join(meme.keywords) if meme.keywords else "无"
                caption = f"随机表情: {meme.key}\n关键词: {aliases}\n"
                trigger_cmd = self._format_plus_one_command(
                    used_prefix, match.matched_trigger
                )
                async for res in self._deliver_meme_image(
                    event,
                    img_bytes,
                    trigger_command=trigger_cmd,
                    caption=caption,
                ):
                    yield res
                event.stop_event()
                return
            except Exception as exc:
                last_err = exc
                logger.warning(f"[meme_rs] 随机表情 {meme.key} 生成失败，尝试下一个: {exc}")

        yield event.plain_result(f"随机表情生成失败: {last_err}")
        event.stop_event()

    # ------------------------------------------------------------------
    # AstrBot 标准指令（支持 /meme列表、/表情列表 等）
    # ------------------------------------------------------------------

    @filter.command("meme列表", alias={"表情列表", "memes列表", "meme_list"}, priority=5)
    async def cmd_list(self, event: AstrMessageEvent):
        """查看所有可用表情包列表图片"""
        async for res in self._handle_list(event):
            yield res

    @filter.command("meme详情", alias={"表情详情", "meme_info"}, priority=5)
    async def cmd_info(self, event: AstrMessageEvent):
        """查看指定表情包的详细用法与预览图"""
        raw = event.get_message_str().strip()
        parts = raw.split(maxsplit=1)
        query = parts[1].strip() if len(parts) > 1 else ""
        async for res in self._handle_info(event, query):
            yield res

    @filter.command("meme搜索", alias={"表情搜索", "meme_search"}, priority=5)
    async def cmd_search(self, event: AstrMessageEvent):
        """按关键词、名称或标签搜索表情包"""
        raw = event.get_message_str().strip()
        parts = raw.split(maxsplit=1)
        query = parts[1].strip() if len(parts) > 1 else ""
        async for res in self._handle_search(event, query):
            yield res

    @filter.command("随机表情", alias={"随机meme", "随机表情包", "meme_random"}, priority=5)
    async def cmd_random(self, event: AstrMessageEvent):
        """随机生成一张表情包"""
        async for res in self._handle_random(event):
            yield res

    @filter.command("meme刷新", alias={"表情刷新", "meme_reload", "更新表情列表"}, priority=5)
    async def cmd_reload(self, event: AstrMessageEvent):
        """重新从 meme-generator-rs 同步表情列表"""
        try:
            count = await self._reload_registry(force=True)
            yield event.plain_result(
                f"已重新同步 meme-generator-rs ({self.client.base_url})，共加载 {count} 个表情。"
            )
        except Exception as exc:
            yield event.plain_result(f"刷新表情列表失败: {exc}")
        event.stop_event()

    async def _handle_example(self, event: AstrMessageEvent):
        text = (
            "表情制作示例：\n"
            "• hello（或 hello @某人 / 回复图片发 hello）\n"
            "• 5000兆 我去/洛天依（多段文本用 / 分隔）\n"
            "• 带参数：表情关键词 #参数名 值 或 --参数名 值（如：对称 #direction top）"
        )
        yield event.plain_result(text)
        event.stop_event()

    async def _handle_help(self, event: AstrMessageEvent):
        prefixes_str = " 或 ".join(self._prefixes)
        prefix_tip_inner = (
            f"当前已开启强制前缀，需加 {prefixes_str} 触发"
            if self._force_prefix
            else f"可直接输入关键词，也可加 {prefixes_str} 前缀触发"
        )
        plain_text = (
            f"表情包制作帮助（已加载 {self.registry.count} 个表情）\n"
            f"（{prefix_tip_inner}）\n\n"
            "常用指令：\n"
            "• /meme列表 — 查看全部表情列表图\n"
            "• /meme搜索 <词> — 搜索表情\n"
            "• /meme详情 <词> — 查看参数与预览图\n"
            "• /随机表情 — 随机制作一张表情\n"
            "• /制作示例 — 查看表情制作示例\n"
            "• /meme刷新 — 重新同步表情库\n"
            "• /meme状态 — 检查服务连通性"
        )
        if self._is_qq_official_event(event):
            md_text = (
                f"### 表情包制作帮助\n"
                f"已加载 **{self.registry.count}** 个表情\n"
                f"> {prefix_tip_inner}\n\n"
                "- `/meme列表` — 查看全部表情列表图\n"
                "- `/meme搜索 <词>` — 搜索表情\n"
                "- `/meme详情 <词>` — 查看参数与预览图\n"
                "- `/随机表情` — 随机制作一张表情\n"
                "- `/制作示例` — 查看表情制作示例\n"
                "- `/meme刷新` — 重新同步表情库\n"
                "- `/meme状态` — 检查服务连通性"
            )
            sent = await send_qq_official_markdown_keyboard(
                event,
                md_text,
                build_help_keyboard(),
                log=logger,
            )
            if sent:
                event.stop_event()
                return
        yield event.plain_result(plain_text)
        event.stop_event()

    @filter.command("meme状态", alias={"表情状态", "meme_status"}, priority=5)
    async def cmd_status(self, event: AstrMessageEvent):
        """检查 meme-generator-rs 服务连通性与延迟"""
        ok, latency_ms, info = await self.client.health_check()
        platform_name = (
            str(event.get_platform_name() or "unknown")
            if hasattr(event, "get_platform_name")
            else "unknown"
        )
        if ok:
            yield event.plain_result(
                f"meme-generator-rs 服务正常\n"
                f"地址: {self.client.base_url}\n"
                f"延迟: {latency_ms:.1f} ms\n"
                f"服务端表情数: {info}\n"
                f"本地已索引数: {self.registry.count}\n"
                f"当前平台: {platform_name}"
            )
        else:
            yield event.plain_result(
                f"meme-generator-rs 服务不可用\n"
                f"地址: {self.client.base_url}\n"
                f"错误: {info}\n"
                f"当前平台: {platform_name}"
            )
        event.stop_event()

    @filter.command("制作示例", alias={"meme示例", "表情示例", "meme_example"}, priority=5)
    async def cmd_example(self, event: AstrMessageEvent):
        """查看表情包制作示例"""
        async for res in self._handle_example(event):
            yield res

    @filter.command("meme帮助", alias={"表情帮助", "meme_help"}, priority=5)
    async def cmd_help(self, event: AstrMessageEvent):
        """查看表情包制作插件帮助"""
        async for res in self._handle_help(event):
            yield res

    # ------------------------------------------------------------------
    # 动态关键词 / 快捷预设监听（支持配置前缀与无前缀触发）
    # ------------------------------------------------------------------

    @filter.event_message_type(filter.EventMessageType.ALL, priority=-10)
    async def on_message(self, event: AstrMessageEvent):
        if not self._enable_meme:
            return

        raw_text, mentions = self._extract_raw_text_and_mentions(event)
        if not raw_text:
            return

        # 提取并剥离配置前缀（如 # 或 /）
        had_prefix = False
        used_prefix = ""
        body = raw_text
        for prefix in self._prefixes:
            if body.startswith(prefix):
                had_prefix = True
                used_prefix = prefix
                body = body[len(prefix) :].strip()
                break

        if not body:
            return

        # 快捷管理触发：支持 #表情列表 / #表情详情 xxx / #表情搜索 xxx / #随机表情
        if re.fullmatch(r"(?:表情|memes?)列表", body, re.IGNORECASE):
            async for res in self._handle_list(event):
                yield res
            return
        m_info = re.fullmatch(r"(?:表情|meme)详情\s*(.+)", body, re.IGNORECASE)
        if m_info:
            async for res in self._handle_info(event, m_info.group(1)):
                yield res
            return
        m_search = re.fullmatch(r"(?:表情|meme)搜索\s*(.+)", body, re.IGNORECASE)
        if m_search:
            async for res in self._handle_search(event, m_search.group(1)):
                yield res
            return
        if re.fullmatch(r"(?:表情)?随机(?:表情|meme)(?:包)?", body, re.IGNORECASE):
            async for res in self._handle_random(event):
                yield res
            return
        if re.fullmatch(r"(?:制作|表情|meme)示例", body, re.IGNORECASE):
            async for res in self._handle_example(event):
                yield res
            return
        if re.fullmatch(r"(?:表情|meme)帮助", body, re.IGNORECASE):
            async for res in self._handle_help(event):
                yield res
            return

        if self._force_prefix and not had_prefix:
            return

        # 若尚未同步，静默尝试自动连接（带 30 秒节流，避免刷日志/阻塞）
        if not await self._ensure_loaded(force_on_empty=False):
            return

        match = self.registry.match_text(body)
        if match is None:
            return

        meme = match.meme
        # 禁用列表检查
        if meme.key in self._get_blacklist_keys():
            logger.info(f"[meme_rs] 表情 {meme.key} 在禁用列表中，跳过生成")
            return

        # 防误触发：不需要文本的表情，如果用户附带了额外无关文字，则忽略不触发
        if meme.min_texts == 0 and meme.max_texts == 0 and match.remaining_text:
            stripped_for_check = AT_TEXT_RE.sub("", match.remaining_text).strip()
            if stripped_for_check:
                try:
                    _, leftover = parse_options(
                        meme, stripped_for_check, preset_options=match.preset_options
                    )
                except MemeParseError:
                    # 非前缀触发且参数解析失败时，多半是日常聊天误撞，直接忽略
                    if not had_prefix:
                        return
                    leftover = ""
                if leftover:
                    return

        try:
            img_bytes = await self._execute_meme(event, match, mentions)
            trigger_cmd = self._format_plus_one_command(
                used_prefix, match.matched_trigger
            )
            async for res in self._deliver_meme_image(
                event, img_bytes, trigger_command=trigger_cmd
            ):
                yield res
            event.stop_event()
        except MemeParseError as exc:
            if self._error_reply:
                yield event.plain_result(f"[表情包制作] {exc}")
            event.stop_event()
        except MemeConnectionError as exc:
            logger.error(f"[meme_rs] 连接失败 ({meme.key}): {exc}")
            if self._error_reply:
                yield event.plain_result(f"[表情包制作] {exc}")
            event.stop_event()
        except MemeClientError as exc:
            logger.error(f"[meme_rs] 生成失败 ({meme.key}): {exc}")
            if self._error_reply:
                yield event.plain_result(f"[表情包制作] 生成表情失败: {exc}")
            event.stop_event()
        except Exception as exc:
            logger.exception(f"[meme_rs] 处理表情 {meme.key} 出现未预期异常: {exc}")
            if self._error_reply:
                yield event.plain_result(f"[表情包制作] 生成表情失败: {exc}")
            event.stop_event()
