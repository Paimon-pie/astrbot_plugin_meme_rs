from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import random
import re
from html import escape
from typing import Any

import httpx

MAX_MATERIAL_IMAGE_BYTES = 10 * 1024 * 1024
IMAGE_TRANSFER_ERR_RE = re.compile(r"(?<!\d)40034141(?!\d)")


def detect_image_meta(img_bytes: bytes) -> tuple[str, str, int, int]:
    """Return (suffix, content_type, width, height) for raw image bytes."""
    if img_bytes.startswith((b"GIF87a", b"GIF89a")):
        suffix, content_type = ".gif", "image/gif"
    elif img_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
        suffix, content_type = ".png", "image/png"
    elif img_bytes.startswith(b"\xff\xd8\xff"):
        suffix, content_type = ".jpg", "image/jpeg"
    elif len(img_bytes) >= 12 and img_bytes[:4] == b"RIFF" and img_bytes[8:12] == b"WEBP":
        suffix, content_type = ".webp", "image/webp"
    else:
        suffix, content_type = ".png", "image/png"

    width, height = 320, 320
    try:
        from PIL import Image as PILImage

        with PILImage.open(io.BytesIO(img_bytes)) as im:
            w, h = im.size
            if w > 0 and h > 0:
                width, height = int(w), int(h)
    except Exception:
        pass

    return suffix, content_type, width, height


def build_input_button(
    button_id: str,
    label: str,
    data: str,
    *,
    style: int = 0,
    enter: bool = False,
) -> dict[str, Any]:
    """Build a single QQ Official type=2 (input-box fill) button."""
    btn_label = (label or "").strip()[:10] or "按钮"
    btn_style = 1 if int(style) == 1 else 0
    return {
        "id": button_id,
        "render_data": {
            "label": btn_label,
            "visited_label": btn_label,
            "style": btn_style,
        },
        "action": {
            "type": 2,
            "data": data,
            "enter": enter,
            "permission": {"type": 2},
            "unsupport_tips": "当前 QQ 版本暂不支持此按钮",
        },
    }


def build_plus_one_keyboard(
    trigger_command: str,
    *,
    label: str = "+1",
    style: int = 0,
) -> dict[str, Any]:
    """Build a QQ Official inline keyboard with a single type=2 (fill input box) button."""
    return {
        "content": {
            "rows": [
                {
                    "buttons": [
                        build_input_button(
                            "meme_plus_one",
                            label or "+1",
                            trigger_command,
                            style=style,
                            enter=False,
                        )
                    ]
                }
            ]
        }
    }


def build_help_keyboard() -> dict[str, Any]:
    """Build the QQ Official help menu keyboard (Row1: 2 blue, Row2: 2 gray, Row3: 1 blue)."""
    return {
        "content": {
            "rows": [
                {
                    "buttons": [
                        build_input_button(
                            "meme_help_list",
                            "meme列表",
                            "/meme列表",
                            style=1,
                            enter=False,
                        ),
                        build_input_button(
                            "meme_help_example",
                            "制作示例",
                            "/制作示例",
                            style=1,
                            enter=False,
                        ),
                    ]
                },
                {
                    "buttons": [
                        build_input_button(
                            "meme_help_search",
                            "meme搜索",
                            "/meme搜索 ",
                            style=0,
                            enter=False,
                        ),
                        build_input_button(
                            "meme_help_info",
                            "meme详情",
                            "/meme详情 ",
                            style=0,
                            enter=False,
                        ),
                    ]
                },
                {
                    "buttons": [
                        build_input_button(
                            "meme_help_random",
                            "随机表情",
                            "/随机表情",
                            style=1,
                            enter=False,
                        ),
                    ]
                },
            ]
        }
    }


def _extract_sender_openid(raw_message: Any, fallback_sender_id: str) -> str:
    author = getattr(raw_message, "author", None)
    for cand in (
        getattr(raw_message, "group_member_openid", None),
        getattr(author, "member_openid", None),
        getattr(raw_message, "user_openid", None),
        getattr(author, "user_openid", None),
        getattr(author, "id", None),
        fallback_sender_id,
    ):
        s = str(cand or "").strip()
        if s:
            return s
    return ""


def _is_image_transfer_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    return (
        IMAGE_TRANSFER_ERR_RE.search(msg) is not None
        or "拉图" in msg
        or (
            "图片" in msg
            and any(word in msg for word in ("转存", "拉取", "下载", "超时"))
        )
        or ("转存" in msg and any(word in msg for word in ("失败", "超时")))
    )


def _media_route(method: str, path: str, **parameters: Any) -> Any:
    from botpy.http import Route

    class _MaterialRoute(Route):
        DOMAIN = "api.bot.qq.com"

    return _MaterialRoute(method, path, **parameters)


async def _upload_temporary_material(
    http: Any,
    img_bytes: bytes,
    suffix: str,
    content_type: str,
) -> dict[str, Any]:
    """Upload bytes to QQ Official material API (prepare -> PUT -> complete)."""
    if not img_bytes:
        raise RuntimeError("图片数据为空")
    if len(img_bytes) > MAX_MATERIAL_IMAGE_BYTES:
        raise RuntimeError("图片超过 10MB，无法上传为 QQ 临时素材")

    md5 = hashlib.md5(img_bytes).hexdigest()
    sha1 = hashlib.sha1(img_bytes).hexdigest()
    file_name = f"meme_{md5[:12]}{suffix}"

    prepare = await http.request(
        _media_route("POST", "/v2/media-images/prepare"),
        json={
            "file_name": file_name,
            "file_size": len(img_bytes),
            "content_type": content_type,
            "md5": md5,
            "sha1": sha1,
        },
    )
    if not isinstance(prepare, dict) or not prepare.get("presigned_url"):
        raise RuntimeError(f"QQ 素材预上传接口返回异常: {prepare}")

    async with httpx.AsyncClient(timeout=httpx.Timeout(45.0, connect=10.0)) as client:
        put_resp = await client.put(
            prepare["presigned_url"],
            content=img_bytes,
            headers={"Content-Type": content_type},
        )
        if put_resp.status_code >= 300:
            raise RuntimeError(f"上传临时素材到 QQ 存储失败: HTTP {put_resp.status_code}")
        etag = str(put_resp.headers.get("ETag") or "").strip('"') or md5

    complete = await http.request(
        _media_route("POST", "/v2/media-images/complete"),
        json={"upload_id": prepare["upload_id"], "md5": etag},
    )
    if not isinstance(complete, dict) or not complete.get("url"):
        raise RuntimeError(f"QQ 素材完成上传接口返回异常: {complete}")
    return complete


async def _delete_temporary_material(http: Any, image_id: str, log: Any = None) -> None:
    if not image_id:
        return
    try:
        await http.request(
            _media_route(
                "DELETE",
                "/v2/media-images/{image_id}/delete",
                image_id=image_id,
            )
        )
    except Exception as exc:
        if log:
            log.warning(f"[meme_rs] 删除 QQ 临时素材 {image_id} 失败: {exc}")


async def _upload_v2_media(
    http: Any,
    img_bytes: bytes,
    *,
    group_openid: str | None = None,
    user_openid: str | None = None,
) -> Any:
    """Upload image bytes via QQ v2 rich-media endpoint for msg_type=7."""
    from botpy.http import Route
    from botpy.types.message import Media

    payload: dict[str, Any] = {
        "file_type": 1,
        "srv_send_msg": False,
        "file_data": base64.b64encode(img_bytes).decode("ascii"),
    }
    if group_openid:
        payload["group_openid"] = group_openid
        route = Route("POST", "/v2/groups/{group_openid}/files", group_openid=group_openid)
    elif user_openid:
        payload["openid"] = user_openid
        route = Route("POST", "/v2/users/{openid}/files", openid=user_openid)
    else:
        raise RuntimeError("缺少 group_openid 或 openid，无法上传富媒体图片")

    result = await http.request(route, json=payload)
    if not isinstance(result, dict) or not result.get("file_info"):
        raise RuntimeError(f"QQ 富媒体图片上传返回异常: {result}")
    return Media(
        file_uuid=result["file_uuid"],
        file_info=result["file_info"],
        ttl=result.get("ttl", 0),
    )


def _resolve_scene_and_sender(api: Any, raw: Any) -> tuple[Any, str, dict[str, Any]]:
    """Return (send_callable, scene_kind, target_kwargs) for QQ Official raw message."""
    group_openid = getattr(raw, "group_openid", None)
    author = getattr(raw, "author", None)
    user_openid = getattr(raw, "user_openid", None) or getattr(author, "user_openid", None)
    channel_id = getattr(raw, "channel_id", None)
    guild_id = getattr(raw, "guild_id", None)

    if group_openid:
        return api.post_group_message, "group", {"group_openid": group_openid}
    if user_openid:
        return api.post_c2c_message, "c2c", {"openid": user_openid}
    if channel_id:
        return api.post_message, "channel", {"channel_id": channel_id}
    if guild_id:
        return api.post_dms, "dms", {"guild_id": guild_id}
    raise RuntimeError("无法识别 QQ 官方消息场景")


async def _send_with_retry(
    send_func: Any,
    target_kwargs: dict[str, Any],
    payload: dict[str, Any],
    *,
    retry_image_transfer: bool = False,
) -> Any:
    attempts = 3 if retry_image_transfer else 1
    current = dict(payload)
    for attempt in range(attempts):
        try:
            res = await send_func(**target_kwargs, **current)
            if res is None:
                raise RuntimeError("QQ 接口返回 None")
            return res
        except Exception as exc:
            if retry_image_transfer and _is_image_transfer_error(exc) and attempt + 1 < attempts:
                if "msg_seq" in current:
                    current["msg_seq"] = (int(current["msg_seq"]) % 9999) + 1
                await asyncio.sleep(0.5 * (attempt + 1))
                continue
            raise
    raise RuntimeError("发送消息失败")


async def _send_markdown_with_button(
    *,
    api: Any,
    raw: Any,
    img_bytes: bytes,
    keyboard: dict[str, Any],
    message_id: str,
    sender_openid: str,
    caption: str | None,
    reply_with_quote: bool,
    log: Any = None,
) -> None:
    send_func, scene, target_kwargs = _resolve_scene_and_sender(api, raw)
    is_v2 = scene in {"group", "c2c"}
    http = getattr(api, "_http", None)
    if http is None:
        raise RuntimeError("无法获取 qq-botpy http 客户端")

    suffix, content_type, width, height = detect_image_meta(img_bytes)
    uploaded = await _upload_temporary_material(http, img_bytes, suffix, content_type)
    image_id = str(uploaded.get("image_id") or "")
    media_url = str(uploaded["url"]).replace(")", "%29")

    def _build_md_text(with_at: bool) -> str:
        parts: list[str] = []
        if with_at and sender_openid and scene in {"group", "channel"}:
            safe_id = escape(sender_openid, quote=True)
            parts.append(f'<qqbot-at-user id="{safe_id}" />')
        if caption and caption.strip():
            parts.append(caption.strip())
        parts.append(f"![表情 #{width}px #{height}px]({media_url})")
        return "\n".join(parts)

    base_payload: dict[str, Any] = {"keyboard": keyboard}
    if message_id:
        base_payload["msg_id"] = message_id
    if is_v2:
        base_payload["msg_type"] = 2
        base_payload["msg_seq"] = random.randint(1, 9000)

    try:
        # QQ 官 Bot 带按钮消息与引用原消息互斥：不附带 message_reference，
        # 若用户开启了“回复时引用原消息”，在群聊/频道里改为 @发送者。
        md_obj: dict[str, Any] = {"content": _build_md_text(with_at=reply_with_quote)}
        if is_v2:
            md_obj["force_verify_image_resource"] = True
        payload = dict(base_payload)
        payload["markdown"] = md_obj
        await _send_with_retry(
            send_func,
            target_kwargs,
            payload,
            retry_image_transfer=is_v2,
        )
    finally:
        # QQ 已在发送阶段转存素材副本，立即清理临时素材不占用 100 张额度
        await _delete_temporary_material(http, image_id, log=log)


async def _send_media_with_button(
    *,
    api: Any,
    raw: Any,
    img_bytes: bytes,
    keyboard: dict[str, Any],
    message_id: str,
    sender_openid: str,
    caption: str | None,
    reply_with_quote: bool,
    log: Any = None,
) -> None:
    send_func, scene, target_kwargs = _resolve_scene_and_sender(api, raw)
    if scene not in {"group", "c2c"}:
        raise RuntimeError("media 模式仅适用于 QQ 官 Bot 群聊或 C2C 私聊")

    http = getattr(api, "_http", None)
    if http is None:
        raise RuntimeError("无法获取 qq-botpy http 客户端")

    media = await _upload_v2_media(
        http,
        img_bytes,
        group_openid=target_kwargs.get("group_openid"),
        user_openid=target_kwargs.get("openid"),
    )

    def _build_content(with_at: bool) -> str | None:
        parts: list[str] = []
        if with_at and sender_openid and scene == "group":
            safe_id = escape(sender_openid, quote=True)
            parts.append(f'<qqbot-at-user id="{safe_id}" />')
        if caption and caption.strip():
            parts.append(caption.strip())
        joined = "\n".join(parts).strip()
        return joined or None

    # QQ 官 Bot 带按钮消息与引用原消息互斥：不附带 message_reference，
    # 若开启了“回复时引用原消息”，在群聊里改为 @发送者。
    payload: dict[str, Any] = {
        "msg_type": 7,
        "media": media,
        "keyboard": keyboard,
        "content": _build_content(with_at=reply_with_quote),
        "msg_seq": random.randint(1, 9000),
    }
    if message_id:
        payload["msg_id"] = message_id

    await _send_with_retry(send_func, target_kwargs, payload)


async def send_qq_official_meme_with_plus_one(
    event: Any,
    img_bytes: bytes,
    trigger_command: str,
    *,
    caption: str | None = None,
    label: str = "+1",
    style: int = 0,
    mode: str = "auto",
    reply_with_quote: bool = False,
    log: Any = None,
) -> bool:
    """Send a meme image with an inline `+1` button in the same QQ Official message.

    Returns True if sent directly via QQ Official API, or False to let caller fall back
    to standard AstrBot image sending.
    """
    raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
    bot = getattr(event, "bot", None)
    api = getattr(bot, "api", None)
    if raw is None or api is None:
        return False

    message_id = str(
        getattr(getattr(event, "message_obj", None), "message_id", None)
        or getattr(raw, "id", None)
        or ""
    )
    sender_id = str(event.get_sender_id() or "") if hasattr(event, "get_sender_id") else ""
    sender_openid = _extract_sender_openid(raw, sender_id)
    keyboard = build_plus_one_keyboard(trigger_command, label=label, style=style)

    normalized_mode = mode if mode in {"auto", "markdown", "media"} else "auto"
    if normalized_mode == "markdown":
        plan = ["markdown"]
    elif normalized_mode == "media":
        plan = ["media"]
    else:
        plan = ["markdown", "media"]

    for step in plan:
        try:
            if step == "markdown":
                await _send_markdown_with_button(
                    api=api,
                    raw=raw,
                    img_bytes=img_bytes,
                    keyboard=keyboard,
                    message_id=message_id,
                    sender_openid=sender_openid,
                    caption=caption,
                    reply_with_quote=reply_with_quote,
                    log=log,
                )
            else:
                await _send_media_with_button(
                    api=api,
                    raw=raw,
                    img_bytes=img_bytes,
                    keyboard=keyboard,
                    message_id=message_id,
                    sender_openid=sender_openid,
                    caption=caption,
                    reply_with_quote=reply_with_quote,
                    log=log,
                )
            try:
                event._has_send_oper = True
            except Exception:
                pass
            return True
        except Exception as exc:
            if log:
                log.warning(
                    f"[meme_rs] QQ 官 Bot {step} 模式发送带 +1 按钮消息失败: {exc}"
                )

    return False


async def send_qq_official_markdown_keyboard(
    event: Any,
    markdown_text: str,
    keyboard: dict[str, Any],
    *,
    log: Any = None,
) -> bool:
    """Send a QQ Official Markdown + inline keyboard message; return False on failure."""
    raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
    bot = getattr(event, "bot", None)
    api = getattr(bot, "api", None)
    if raw is None or api is None:
        return False

    message_id = str(
        getattr(getattr(event, "message_obj", None), "message_id", None)
        or getattr(raw, "id", None)
        or ""
    )
    try:
        send_func, scene, target_kwargs = _resolve_scene_and_sender(api, raw)
        is_v2 = scene in {"group", "c2c"}
        payload: dict[str, Any] = {
            "markdown": {"content": markdown_text},
            "keyboard": keyboard,
        }
        if message_id:
            payload["msg_id"] = message_id
        if is_v2:
            payload["msg_type"] = 2
            payload["msg_seq"] = random.randint(1, 9000)
        await _send_with_retry(send_func, target_kwargs, payload)
        try:
            event._has_send_oper = True
        except Exception:
            pass
        return True
    except Exception as exc:
        if log:
            log.warning(f"[meme_rs] QQ 官 Bot Markdown+按钮消息发送失败，回退纯文本: {exc}")
        return False
