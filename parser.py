from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any


class MemeParseError(Exception):
    """Raised when user arguments for a matched meme are invalid."""


@dataclass
class MemeInfo:
    key: str
    keywords: list[str] = field(default_factory=list)
    shortcuts: list[dict[str, Any]] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    min_images: int = 0
    max_images: int = 0
    min_texts: int = 0
    max_texts: int = 0
    default_texts: list[str] = field(default_factory=list)
    options: list[dict[str, Any]] = field(default_factory=list)
    presets: list[str] = field(default_factory=list)

    @classmethod
    def from_raw(cls, raw: dict[str, Any]) -> "MemeInfo":
        params = raw.get("params") or {}
        return cls(
            key=str(raw.get("key", "")),
            keywords=[str(k) for k in (raw.get("keywords") or []) if k],
            shortcuts=list(raw.get("shortcuts") or []),
            tags=[str(t) for t in (raw.get("tags") or []) if t],
            min_images=int(params.get("min_images") or 0),
            max_images=int(params.get("max_images") or 0),
            min_texts=int(params.get("min_texts") or 0),
            max_texts=int(params.get("max_texts") or 0),
            default_texts=[str(t) for t in (params.get("default_texts") or []) if t is not None],
            options=list(params.get("options") or []),
        )


@dataclass
class _CompiledShortcut:
    key: str
    pattern_str: str
    regex: re.Pattern[str]
    texts: list[str]
    names: list[str]
    options: dict[str, Any]
    humanized: str | None = None


@dataclass
class MemeMatch:
    meme: MemeInfo
    matched_trigger: str
    remaining_text: str
    preset_options: dict[str, Any] = field(default_factory=dict)
    preset_texts: list[str] = field(default_factory=list)
    preset_names: list[str] = field(default_factory=list)
    is_shortcut: bool = False


class _SafeFormatDict(dict[str, str]):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


ASCII_ALNUM_RE = re.compile(r"[A-Za-z0-9]")
TRUE_VALUES = {"true", "真", "是", "yes", "y", "1", "on"}
FALSE_VALUES = {"false", "假", "否", "no", "n", "0", "off"}

# 匹配 `#opt value` 与 CLI 风格 `--opt value` / `--opt=value`
OPTION_TOKEN_RE = re.compile(
    r"(?:^|\s)(?:#([^\s#=]+)|--([^\s#=]+)(?:=(\S+))?)(?=\s|$)"
)


class MemeRegistry:
    """In-memory index of meme-generator-rs memes, keywords, shortcuts, and presets."""

    def __init__(self) -> None:
        self.memes_by_key: dict[str, MemeInfo] = {}
        self.keyword_to_key: dict[str, str] = {}
        self.preset_to_key: dict[str, str] = {}
        self._keyword_regex: re.Pattern[str] | None = None
        self._shortcuts: list[_CompiledShortcut] = []

    @property
    def count(self) -> int:
        return len(self.memes_by_key)

    def load(
        self,
        raw_infos: list[dict[str, Any]],
        *,
        enable_shortcuts: bool = True,
    ) -> int:
        memes_by_key: dict[str, MemeInfo] = {}
        keyword_to_key: dict[str, str] = {}
        preset_to_key: dict[str, str] = {}
        shortcuts: list[_CompiledShortcut] = []
        seen_shortcut_patterns: set[str] = set()

        for raw in raw_infos:
            info = MemeInfo.from_raw(raw)
            if not info.key:
                continue
            memes_by_key[info.key] = info
            for kw in info.keywords:
                kw_norm = kw.strip().lower()
                if kw_norm and kw_norm not in keyword_to_key:
                    keyword_to_key[kw_norm] = info.key

            if enable_shortcuts:
                for sc in info.shortcuts:
                    pattern = str(sc.get("pattern") or "").strip()
                    if not pattern:
                        continue
                    try:
                        compiled = re.compile(
                            f"^(?P<__trigger__>{pattern})(?P<__rest__>.*)$",
                            re.IGNORECASE | re.DOTALL,
                        )
                    except re.error:
                        continue
                    shortcuts.append(
                        _CompiledShortcut(
                            key=info.key,
                            pattern_str=pattern,
                            regex=compiled,
                            texts=[str(t) for t in (sc.get("texts") or [])],
                            names=[str(n) for n in (sc.get("names") or [])],
                            options=dict(sc.get("options") or {}),
                            humanized=sc.get("humanized"),
                        )
                    )
                    seen_shortcut_patterns.add(pattern.lower())
                    label = sc.get("humanized") or pattern
                    if label not in info.presets:
                        info.presets.append(str(label))

        # 最长关键词优先，避免短关键词抢先匹配
        all_keywords = sorted(
            (kw for info in memes_by_key.values() for kw in info.keywords if kw.strip()),
            key=len,
            reverse=True,
        )
        dedup_keywords: list[str] = []
        seen_kw: set[str] = set()
        for kw in all_keywords:
            low = kw.strip().lower()
            if low not in seen_kw:
                seen_kw.add(low)
                dedup_keywords.append(kw.strip())

        if dedup_keywords:
            escaped = "|".join(re.escape(kw) for kw in dedup_keywords)
            keyword_regex = re.compile(
                f"^({escaped})(.*)$",
                re.IGNORECASE | re.DOTALL,
            )
        else:
            keyword_regex = None

        # 较长的 shortcut pattern 优先
        shortcuts.sort(key=lambda item: len(item.pattern_str), reverse=True)

        self.memes_by_key = memes_by_key
        self.keyword_to_key = keyword_to_key
        self.preset_to_key = preset_to_key
        self._keyword_regex = keyword_regex
        self._shortcuts = shortcuts
        return len(memes_by_key)

    def resolve_key(self, name_or_key: str) -> str | None:
        query = name_or_key.strip()
        if not query:
            return None
        if query in self.memes_by_key:
            return query
        low = query.lower()
        for key in self.memes_by_key:
            if key.lower() == low:
                return key
        if low in self.keyword_to_key:
            return self.keyword_to_key[low]
        if low in self.preset_to_key:
            return self.preset_to_key[low]
        for sc in self._shortcuts:
            if sc.pattern_str.lower() == low or (sc.humanized and sc.humanized.lower() == low):
                return sc.key
        return None

    def get_info(self, name_or_key: str) -> MemeInfo | None:
        key = self.resolve_key(name_or_key)
        return self.memes_by_key.get(key) if key else None

    @staticmethod
    def _has_ascii_word_collision(trigger: str, rest: str) -> bool:
        """Prevent ASCII keyword like 'hello' from matching 'helloworld'."""
        if not trigger or not rest:
            return False
        return bool(ASCII_ALNUM_RE.fullmatch(trigger[-1]) and ASCII_ALNUM_RE.fullmatch(rest[0]))

    def match_text(self, text: str) -> MemeMatch | None:
        candidate = text.strip()
        if not candidate:
            return None

        kw_match: MemeMatch | None = None
        if self._keyword_regex is not None:
            m = self._keyword_regex.match(candidate)
            if m:
                trigger, rest = m.group(1), m.group(2)
                if not self._has_ascii_word_collision(trigger, rest):
                    key = self.keyword_to_key.get(trigger.lower())
                    meme = self.memes_by_key.get(key) if key else None
                    if meme is not None:
                        kw_match = MemeMatch(
                            meme=meme,
                            matched_trigger=trigger,
                            remaining_text=rest.strip(),
                            is_shortcut=False,
                        )

        sc_match: MemeMatch | None = None
        for sc in self._shortcuts:
            m = sc.regex.match(candidate)
            if not m:
                continue
            trigger = m.group("__trigger__")
            rest = m.group("__rest__")
            if self._has_ascii_word_collision(trigger, rest):
                continue
            meme = self.memes_by_key.get(sc.key)
            if meme is None:
                continue
            group_dict = {
                k: v
                for k, v in m.groupdict().items()
                if k not in {"__trigger__", "__rest__"} and v is not None
            }
            fmt_ctx = _SafeFormatDict(group_dict)
            formatted_texts = [t.format_map(fmt_ctx) for t in sc.texts]
            formatted_names = [n.format_map(fmt_ctx) for n in sc.names]
            sc_match = MemeMatch(
                meme=meme,
                matched_trigger=trigger,
                remaining_text=rest.strip(),
                preset_options=dict(sc.options),
                preset_texts=formatted_texts,
                preset_names=formatted_names,
                is_shortcut=True,
            )
            break

        if kw_match and sc_match:
            if len(sc_match.matched_trigger) > len(kw_match.matched_trigger):
                return sc_match
            return kw_match
        return kw_match or sc_match

    def search_local(self, query: str) -> list[MemeInfo]:
        q = query.strip().lower()
        if not q:
            return []
        exact: list[MemeInfo] = []
        partial: list[MemeInfo] = []
        seen: set[str] = set()

        for info in self.memes_by_key.values():
            haystack = [
                info.key.lower(),
                *(kw.lower() for kw in info.keywords),
                *(tag.lower() for tag in info.tags),
                *(p.lower() for p in info.presets),
            ]
            if any(h == q for h in haystack):
                if info.key not in seen:
                    seen.add(info.key)
                    exact.append(info)
            elif any(q in h for h in haystack):
                if info.key not in seen:
                    seen.add(info.key)
                    partial.append(info)

        return exact + partial


def _find_option_spec(
    meme: MemeInfo, raw_name: str
) -> dict[str, Any] | None:
    target = raw_name.strip().lower()
    for opt in meme.options:
        name = str(opt.get("name", "")).lower()
        if name == target:
            return opt
        flags = opt.get("parser_flags") or {}
        long_aliases = [str(a).lower() for a in (flags.get("long_aliases") or [])]
        short_aliases = [str(a).lower() for a in (flags.get("short_aliases") or [])]
        if target in long_aliases or target in short_aliases:
            return opt
    return None


def _convert_option_value(opt_spec: dict[str, Any], raw_value: str) -> Any:
    opt_name = str(opt_spec.get("name", ""))
    opt_type = str(opt_spec.get("type", ""))
    val_str = raw_value.strip()

    if opt_type == "boolean":
        low = val_str.lower()
        if low in TRUE_VALUES:
            return True
        if low in FALSE_VALUES:
            return False
        raise MemeParseError(f"参数 {opt_name} 需要是布尔值（true/false/是/否/1/0）")

    if opt_type == "integer":
        try:
            int_val = int(val_str)
        except ValueError as exc:
            raise MemeParseError(f"参数 {opt_name} 需要是整数") from exc
        minimum = opt_spec.get("minimum")
        maximum = opt_spec.get("maximum")
        if minimum is not None and int_val < minimum:
            raise MemeParseError(f"参数 {opt_name} 不能小于 {minimum}")
        if maximum is not None and int_val > maximum:
            raise MemeParseError(f"参数 {opt_name} 不能大于 {maximum}")
        return int_val

    if opt_type == "float":
        try:
            float_val = float(val_str)
        except ValueError as exc:
            raise MemeParseError(f"参数 {opt_name} 需要是数字") from exc
        minimum = opt_spec.get("minimum")
        maximum = opt_spec.get("maximum")
        if minimum is not None and float_val < minimum:
            raise MemeParseError(f"参数 {opt_name} 不能小于 {minimum}")
        if maximum is not None and float_val > maximum:
            raise MemeParseError(f"参数 {opt_name} 不能大于 {maximum}")
        return float_val

    if opt_type == "string":
        choices = opt_spec.get("choices")
        if choices and val_str not in choices:
            raise MemeParseError(
                f"参数 {opt_name} 的值必须是以下之一: {', '.join(map(str, choices))}"
            )
        return val_str

    raise MemeParseError(f"不支持的参数类型: {opt_type}")


def parse_options(
    meme: MemeInfo,
    user_text: str,
    preset_options: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], str]:
    """Extract `#opt val` and `--opt val` options from user_text and validate them."""
    options: dict[str, Any] = dict(preset_options or {})
    if not user_text:
        return options, ""

    # 快速检查是否包含选项标记
    if "#" not in user_text and "--" not in user_text:
        return options, user_text.strip()

    tokens = user_text.strip().split()
    kept_tokens: list[str] = []
    idx = 0
    n = len(tokens)

    while idx < n:
        tok = tokens[idx]
        raw_opt_name: str | None = None
        inline_val: str | None = None

        if tok.startswith("--") and len(tok) > 2:
            body = tok[2:]
            if "=" in body:
                raw_opt_name, inline_val = body.split("=", 1)
            else:
                raw_opt_name = body
        elif tok.startswith("#") and len(tok) > 1:
            body = tok[1:]
            if "=" in body:
                raw_opt_name, inline_val = body.split("=", 1)
            else:
                raw_opt_name = body

        if raw_opt_name is None:
            kept_tokens.append(tok)
            idx += 1
            continue

        spec = _find_option_spec(meme, raw_opt_name)
        if spec is None:
            # 如果该表情本身支持选项，明确提示不支持该参数；
            # 若该表情没有任何选项，把 #xxx 当普通文本保留（避免误伤带 # 的文本内容）
            if meme.options:
                supported = ", ".join(str(o.get("name", "")) for o in meme.options)
                raise MemeParseError(
                    f"该表情不支持参数 {raw_opt_name}（可选参数: {supported}）"
                )
            kept_tokens.append(tok)
            idx += 1
            continue

        opt_key = str(spec["name"])
        opt_type = str(spec.get("type", ""))

        if inline_val is not None:
            options[opt_key] = _convert_option_value(spec, inline_val)
            idx += 1
            continue

        if opt_type == "boolean":
            if idx + 1 < n and tokens[idx + 1].lower() in (TRUE_VALUES | FALSE_VALUES):
                options[opt_key] = _convert_option_value(spec, tokens[idx + 1])
                idx += 2
            else:
                options[opt_key] = True
                idx += 1
            continue

        if idx + 1 >= n:
            raise MemeParseError(f"参数 {opt_key} 缺少参数值")

        options[opt_key] = _convert_option_value(spec, tokens[idx + 1])
        idx += 2

    return options, " ".join(kept_tokens).strip()


def parse_texts(
    meme: MemeInfo,
    user_text: str,
    preset_texts: list[str] | None = None,
) -> list[str]:
    """Parse texts separated by `/` and fill defaults when needed."""
    if meme.min_texts == 0 and meme.max_texts == 0:
        return []

    texts: list[str] = list(preset_texts or [])
    cleaned = user_text.strip()
    if cleaned:
        # 支持 \/ 转义字面斜杠
        placeholder = "\x00SLASH\x00"
        parts = cleaned.replace(r"\/", placeholder).split("/")
        for part in parts:
            val = part.replace(placeholder, "/").strip()
            if val:
                texts.append(val)

    # 如果文本不足，优先按顺序补齐 default_texts
    if len(texts) < meme.min_texts and meme.default_texts:
        defaults = meme.default_texts
        start_idx = len(texts)
        while len(texts) < meme.min_texts:
            texts.append(defaults[start_idx % len(defaults)])
            start_idx += 1

    if meme.max_texts > 0 and len(texts) > meme.max_texts:
        texts = texts[: meme.max_texts]

    if len(texts) < meme.min_texts:
        if meme.min_texts == meme.max_texts:
            raise MemeParseError(f"该表情需要 {meme.min_texts} 段文本（多段请用 / 分隔）")
        raise MemeParseError(
            f"该表情需要 {meme.min_texts} ~ {meme.max_texts} 段文本（多段请用 / 分隔）"
        )

    return texts
