# 中文维护说明：解析 TOML 文档并报告语法错误；此文件随 tomli 上游维护，保留版权与兼容行为。
# 注释规范：docs/COMMENTING_GUIDE.md；修改行为时同步更新边界回归。
from __future__ import annotations
# SPDX-License-Identifier: MIT
# SPDX-FileCopyrightText: 2021 Taneli Hukkinen
# Licensed to PSF under a Contributor Agreement.

from __future__ import annotations

from collections.abc import Iterable
import string
import sys
from types import MappingProxyType
from typing import IO, Any, Final, NamedTuple
import warnings

from ._re import (
    RE_DATETIME,
    RE_LOCALTIME,
    RE_NUMBER,
    match_to_datetime,
    match_to_localtime,
    match_to_number,
)
from ._types import Key, ParseFloat, Pos

# Inline tables/arrays are implemented using recursion. Pathologically
# nested documents cause pure Python to raise RecursionError (which is OK),
# but mypyc binary wheels will crash unrecoverably (not OK). According to
# mypyc docs this will be fixed in the future:
# https://mypyc.readthedocs.io/en/latest/differences_from_python.html#stack-overflows
# Before mypyc's fix is in, recursion needs to be limited by this library.
# Choosing `sys.getrecursionlimit()` as maximum inline table/array nesting
# level, as it allows more nesting than pure Python, but still seems a far
# lower number than where mypyc binaries crash.
MAX_INLINE_NESTING: Final = sys.getrecursionlimit()

ASCII_CTRL: Final = frozenset(chr(i) for i in range(32)) | frozenset(chr(127))

# Neither of these sets include quotation mark or backslash. They are
# currently handled as separate cases in the parser functions.
ILLEGAL_BASIC_STR_CHARS: Final = ASCII_CTRL - frozenset("\t")
ILLEGAL_MULTILINE_BASIC_STR_CHARS: Final = ASCII_CTRL - frozenset("\t\n")

ILLEGAL_LITERAL_STR_CHARS: Final = ILLEGAL_BASIC_STR_CHARS
ILLEGAL_MULTILINE_LITERAL_STR_CHARS: Final = ILLEGAL_MULTILINE_BASIC_STR_CHARS

ILLEGAL_COMMENT_CHARS: Final = ILLEGAL_BASIC_STR_CHARS

TOML_WS: Final = frozenset(" \t")
TOML_WS_AND_NEWLINE: Final = TOML_WS | frozenset("\n")
BARE_KEY_CHARS: Final = frozenset(string.ascii_letters + string.digits + "-_")
KEY_INITIAL_CHARS: Final = BARE_KEY_CHARS | frozenset("\"'")
HEXDIGIT_CHARS: Final = frozenset(string.hexdigits)

BASIC_STR_ESCAPE_REPLACEMENTS: Final = MappingProxyType(
    {
        "\\b": "\u0008",  # backspace
        "\\t": "\u0009",  # tab
        "\\n": "\u000A",  # linefeed
        "\\f": "\u000C",  # form feed
        "\\r": "\u000D",  # carriage return
        '\\"': "\u0022",  # quote
        "\\\\": "\u005C",  # backslash
    }
)


class DEPRECATED_DEFAULT:
    """Sentinel to be used as default arg during deprecation
    period of TOMLDecodeError's free-form arguments."""


class TOMLDecodeError(ValueError):
    """An error raised if a document is not valid TOML.

    Adds the following attributes to ValueError:
    msg: The unformatted error message
    doc: The TOML document being parsed
    pos: The index of doc where parsing failed
    lineno: The line corresponding to pos
    colno: The column corresponding to pos
    """

    def __init__(
        self,
        msg: str | type[DEPRECATED_DEFAULT] = DEPRECATED_DEFAULT,
        doc: str | type[DEPRECATED_DEFAULT] = DEPRECATED_DEFAULT,
        pos: Pos | type[DEPRECATED_DEFAULT] = DEPRECATED_DEFAULT,
        *args: Any,
    ):
        """构造带文档位置及行列号的 TOML 错误；保留 vendored 上游异常调用语义。"""
        if (
            args
            or not isinstance(msg, str)
            or not isinstance(doc, str)
            or not isinstance(pos, int)
        ):
            warnings.warn(
                "Free-form arguments for TOMLDecodeError are deprecated. "
                "Please set 'msg' (str), 'doc' (str) and 'pos' (int) arguments only.",
                DeprecationWarning,
                stacklevel=2,
            )
            if pos is not DEPRECATED_DEFAULT:
                args = pos, *args
            if doc is not DEPRECATED_DEFAULT:
                args = doc, *args
            if msg is not DEPRECATED_DEFAULT:
                args = msg, *args
            ValueError.__init__(self, *args)
            return

        lineno = doc.count("\n", 0, pos) + 1
        if lineno == 1:
            colno = pos + 1
        else:
            colno = pos - doc.rindex("\n", 0, pos)

        if pos >= len(doc):
            coord_repr = "end of document"
        else:
            coord_repr = f"line {lineno}, column {colno}"
        errmsg = f"{msg} (at {coord_repr})"
        ValueError.__init__(self, errmsg)

        self.msg = msg
        self.doc = doc
        self.pos = pos
        self.lineno = lineno
        self.colno = colno


def load(__fp: IO[bytes], *, parse_float: ParseFloat = float) -> dict[str, Any]:
    """从二进制文件对象读取 UTF-8 TOML；文本流输入会报类型错误。

    Parse TOML from a binary file object."""
    b = __fp.read()
    try:
        s = b.decode()
    except AttributeError:
        raise TypeError(
            "File must be opened in binary mode, e.g. use `open('foo.toml', 'rb')`"
        ) from None
    return loads(s, parse_float=parse_float)


def loads(__s: str, *, parse_float: ParseFloat = float) -> dict[str, Any]:  # noqa: C901
    """逐语句解析 TOML 字符串并返回嵌套字典，错误包含位置。

    Parse TOML from a string."""

    # The spec allows converting "\r\n" to "\n", even in string
    # literals. Let's do so to simplify parsing.
    try:
        src = __s.replace("\r\n", "\n")
    except (AttributeError, TypeError):
        raise TypeError(
            f"Expected str object, not '{type(__s).__qualname__}'"
        ) from None
    pos = 0
    out = Output(NestedDict(), Flags())
    header: Key = ()
    parse_float = make_safe_parse_float(parse_float)

    # Parse one statement at a time
    # (typically means one line in TOML source)
    while True:
        # 1. Skip line leading whitespace
        pos = skip_chars(src, pos, TOML_WS)

        # 2. Parse rules. Expect one of the following:
        #    - end of file
        #    - end of line
        #    - comment
        #    - key/value pair
        #    - append dict to list (and move to its namespace)
        #    - create dict (and move to its namespace)
        # Skip trailing whitespace when applicable.
        try:
            char = src[pos]
        except IndexError:
            break
        if char == "\n":
            pos += 1
            continue
        if char in KEY_INITIAL_CHARS:
            pos = key_value_rule(src, pos, out, header, parse_float)
            pos = skip_chars(src, pos, TOML_WS)
        elif char == "[":
            try:
                second_char: str | None = src[pos + 1]
            except IndexError:
                second_char = None
            out.flags.finalize_pending()
            if second_char == "[":
                pos, header = create_list_rule(src, pos, out)
            else:
                pos, header = create_dict_rule(src, pos, out)
            pos = skip_chars(src, pos, TOML_WS)
        elif char != "#":
            raise TOMLDecodeError("Invalid statement", src, pos)

        # 3. Skip comment
        pos = skip_comment(src, pos)

        # 4. Expect end of line or end of file
        try:
            char = src[pos]
        except IndexError:
            break
        if char != "\n":
            raise TOMLDecodeError(
                "Expected newline or end of document after a statement", src, pos
            )
        pos += 1

    return out.data.dict


class Flags:
    """Flags that map to parsed keys/namespaces."""

    # Marks an immutable namespace (inline array or inline table).
    FROZEN: Final = 0
    # Marks a nest that has been explicitly created and can no longer
    # be opened using the "[table]" syntax.
    EXPLICIT_NEST: Final = 1

    def __init__(self) -> None:
        """初始化命名空间标记树和待提交标记集合。"""
        self._flags: dict[str, dict] = {}
        self._pending_flags: set[tuple[Key, int]] = set()

    def add_pending(self, key: Key, flag: int) -> None:
        """暂存当前键的命名空间标记，延迟到下一表声明提交。"""
        self._pending_flags.add((key, flag))

    def finalize_pending(self) -> None:
        """把待提交标记写入树并清空暂存集合。"""
        for key, flag in self._pending_flags:
            self.set(key, flag, recursive=False)
        self._pending_flags.clear()

    def unset_all(self, key: Key) -> None:
        """移除指定键下全部标记，供新数组表元素使用独立命名空间。"""
        cont = self._flags
        for k in key[:-1]:
            if k not in cont:
                return
            cont = cont[k]["nested"]
        cont.pop(key[-1], None)

    def set(self, key: Key, flag: int, *, recursive: bool) -> None:  # noqa: A003
        """为指定键登记普通或递归生效的标记。"""
        cont = self._flags
        key_parent, key_stem = key[:-1], key[-1]
        for k in key_parent:
            if k not in cont:
                cont[k] = {"flags": set(), "recursive_flags": set(), "nested": {}}
            cont = cont[k]["nested"]
        if key_stem not in cont:
            cont[key_stem] = {"flags": set(), "recursive_flags": set(), "nested": {}}
        cont[key_stem]["recursive_flags" if recursive else "flags"].add(flag)

    def is_(self, key: Key, flag: int) -> bool:
        """查询当前键或祖先递归标记是否包含指定限制。"""
        if not key:
            return False  # document root has no flags
        cont = self._flags
        for k in key[:-1]:
            if k not in cont:
                return False
            inner_cont = cont[k]
            if flag in inner_cont["recursive_flags"]:
                return True
            cont = inner_cont["nested"]
        key_stem = key[-1]
        if key_stem in cont:
            inner_cont = cont[key_stem]
            return flag in inner_cont["flags"] or flag in inner_cont["recursive_flags"]
        return False


class NestedDict:
    def __init__(self) -> None:
        # The parsed content of the TOML document
        """初始化用于保存已解析 TOML 值的根字典。"""
        self.dict: dict[str, Any] = {}

    def get_or_create_nest(
        self,
        key: Key,
        *,
        access_lists: bool = True,
    ) -> dict:
        """沿键路径创建字典；允许时进入数组最后元素，碰到标量时报 KeyError。"""
        cont: Any = self.dict
        for k in key:
            if k not in cont:
                cont[k] = {}
            cont = cont[k]
            if access_lists and isinstance(cont, list):
                cont = cont[-1]
            if not isinstance(cont, dict):
                raise KeyError("There is no nest behind this key")
        return cont

    def append_nest_to_list(self, key: Key) -> None:
        """向指定数组表追加空字典；已有非数组值时报 KeyError。"""
        cont = self.get_or_create_nest(key[:-1])
        last_key = key[-1]
        if last_key in cont:
            list_ = cont[last_key]
            if not isinstance(list_, list):
                raise KeyError("An object other than list found behind this key")
            list_.append({})
        else:
            cont[last_key] = [{}]


class Output(NamedTuple):
    data: NestedDict
    flags: Flags


def skip_chars(src: str, pos: Pos, chars: Iterable[str]) -> Pos:
    """跳过连续属于给定集合的字符，返回首个未跳过的位置。"""
    try:
        while src[pos] in chars:
            pos += 1
    except IndexError:
        pass
    return pos


def skip_until(
    src: str,
    pos: Pos,
    expect: str,
    *,
    error_on: frozenset[str],
    error_on_eof: bool,
) -> Pos:
    """定位结束分隔符并检查途经非法字符；按选项处理文档结束。"""
    try:
        new_pos = src.index(expect, pos)
    except ValueError:
        new_pos = len(src)
        if error_on_eof:
            raise TOMLDecodeError(f"Expected {expect!r}", src, new_pos) from None

    if not error_on.isdisjoint(src[pos:new_pos]):
        while src[pos] not in error_on:
            pos += 1
        raise TOMLDecodeError(f"Found invalid character {src[pos]!r}", src, pos)
    return new_pos


def skip_comment(src: str, pos: Pos) -> Pos:
    """跳过当前位置的注释并拒绝注释中的非法控制字符。"""
    try:
        char: str | None = src[pos]
    except IndexError:
        char = None
    if char == "#":
        return skip_until(
            src, pos + 1, "\n", error_on=ILLEGAL_COMMENT_CHARS, error_on_eof=False
        )
    return pos


def skip_comments_and_array_ws(src: str, pos: Pos) -> Pos:
    """跳过数组语法允许的注释、空格和换行。"""
    while True:
        pos_before_skip = pos
        pos = skip_chars(src, pos, TOML_WS_AND_NEWLINE)
        pos = skip_comment(src, pos)
        if pos == pos_before_skip:
            return pos


def create_dict_rule(src: str, pos: Pos, out: Output) -> tuple[Pos, Key]:
    """解析表头并标记显式命名空间；重复声明及覆盖已有值报错。"""
    pos += 1  # Skip "["
    pos = skip_chars(src, pos, TOML_WS)
    pos, key = parse_key(src, pos)

    if out.flags.is_(key, Flags.EXPLICIT_NEST) or out.flags.is_(key, Flags.FROZEN):
        raise TOMLDecodeError(f"Cannot declare {key} twice", src, pos)
    out.flags.set(key, Flags.EXPLICIT_NEST, recursive=False)
    try:
        out.data.get_or_create_nest(key)
    except KeyError:
        raise TOMLDecodeError("Cannot overwrite a value", src, pos) from None

    if not src.startswith("]", pos):
        raise TOMLDecodeError(
            "Expected ']' at the end of a table declaration", src, pos
        )
    return pos + 1, key


def create_list_rule(src: str, pos: Pos, out: Output) -> tuple[Pos, Key]:
    """解析数组表头并追加新表元素，拒绝修改冻结命名空间。"""
    pos += 2  # Skip "[["
    pos = skip_chars(src, pos, TOML_WS)
    pos, key = parse_key(src, pos)

    if out.flags.is_(key, Flags.FROZEN):
        raise TOMLDecodeError(f"Cannot mutate immutable namespace {key}", src, pos)
    # Free the namespace now that it points to another empty list item...
    out.flags.unset_all(key)
    # ...but this key precisely is still prohibited from table declaration
    out.flags.set(key, Flags.EXPLICIT_NEST, recursive=False)
    try:
        out.data.append_nest_to_list(key)
    except KeyError:
        raise TOMLDecodeError("Cannot overwrite a value", src, pos) from None

    if not src.startswith("]]", pos):
        raise TOMLDecodeError(
            "Expected ']]' at the end of an array declaration", src, pos
        )
    return pos + 2, key


def key_value_rule(
    src: str, pos: Pos, out: Output, header: Key, parse_float: ParseFloat
) -> Pos:
    """把键值写入当前表，校验重复键、命名空间重定义和冻结约束。"""
    pos, key, value = parse_key_value_pair(src, pos, parse_float, nest_lvl=0)
    key_parent, key_stem = key[:-1], key[-1]
    abs_key_parent = header + key_parent

    relative_path_cont_keys = (header + key[:i] for i in range(1, len(key)))
    for cont_key in relative_path_cont_keys:
        # Check that dotted key syntax does not redefine an existing table
        if out.flags.is_(cont_key, Flags.EXPLICIT_NEST):
            raise TOMLDecodeError(f"Cannot redefine namespace {cont_key}", src, pos)
        # Containers in the relative path can't be opened with the table syntax or
        # dotted key/value syntax in following table sections.
        out.flags.add_pending(cont_key, Flags.EXPLICIT_NEST)

    if out.flags.is_(abs_key_parent, Flags.FROZEN):
        raise TOMLDecodeError(
            f"Cannot mutate immutable namespace {abs_key_parent}", src, pos
        )

    try:
        nest = out.data.get_or_create_nest(abs_key_parent)
    except KeyError:
        raise TOMLDecodeError("Cannot overwrite a value", src, pos) from None
    if key_stem in nest:
        raise TOMLDecodeError("Cannot overwrite a value", src, pos)
    # Mark inline table and array namespaces recursively immutable
    if isinstance(value, (dict, list)):
        out.flags.set(header + key, Flags.FROZEN, recursive=True)
    nest[key_stem] = value
    return pos


def parse_key_value_pair(
    src: str, pos: Pos, parse_float: ParseFloat, nest_lvl: int
) -> tuple[Pos, Key, Any]:
    """解析键、等号及值，返回结束位置和键值。"""
    pos, key = parse_key(src, pos)
    try:
        char: str | None = src[pos]
    except IndexError:
        char = None
    if char != "=":
        raise TOMLDecodeError("Expected '=' after a key in a key/value pair", src, pos)
    pos += 1
    pos = skip_chars(src, pos, TOML_WS)
    pos, value = parse_value(src, pos, parse_float, nest_lvl)
    return pos, key, value


def parse_key(src: str, pos: Pos) -> tuple[Pos, Key]:
    """解析带空白的点分键路径，返回字符串元组。"""
    pos, key_part = parse_key_part(src, pos)
    key: Key = (key_part,)
    pos = skip_chars(src, pos, TOML_WS)
    while True:
        try:
            char: str | None = src[pos]
        except IndexError:
            char = None
        if char != ".":
            return pos, key
        pos += 1
        pos = skip_chars(src, pos, TOML_WS)
        pos, key_part = parse_key_part(src, pos)
        key += (key_part,)
        pos = skip_chars(src, pos, TOML_WS)


def parse_key_part(src: str, pos: Pos) -> tuple[Pos, str]:
    """解析一个裸键、字面量键或基本字符串键。"""
    try:
        char: str | None = src[pos]
    except IndexError:
        char = None
    if char in BARE_KEY_CHARS:
        start_pos = pos
        pos = skip_chars(src, pos, BARE_KEY_CHARS)
        return pos, src[start_pos:pos]
    if char == "'":
        return parse_literal_str(src, pos)
    if char == '"':
        return parse_one_line_basic_str(src, pos)
    raise TOMLDecodeError("Invalid initial character for a key part", src, pos)


def parse_one_line_basic_str(src: str, pos: Pos) -> tuple[Pos, str]:
    """跳过起始引号并进入单行基本字符串解析。"""
    pos += 1
    return parse_basic_str(src, pos, multiline=False)


def parse_array(
    src: str, pos: Pos, parse_float: ParseFloat, nest_lvl: int
) -> tuple[Pos, list]:
    """解析逗号分隔的数组，允许尾逗号并检查闭合括号。"""
    pos += 1
    array: list = []

    pos = skip_comments_and_array_ws(src, pos)
    if src.startswith("]", pos):
        return pos + 1, array
    while True:
        pos, val = parse_value(src, pos, parse_float, nest_lvl)
        array.append(val)
        pos = skip_comments_and_array_ws(src, pos)

        c = src[pos : pos + 1]
        if c == "]":
            return pos + 1, array
        if c != ",":
            raise TOMLDecodeError("Unclosed array", src, pos)
        pos += 1

        pos = skip_comments_and_array_ws(src, pos)
        if src.startswith("]", pos):
            return pos + 1, array


def parse_inline_table(
    src: str, pos: Pos, parse_float: ParseFloat, nest_lvl: int
) -> tuple[Pos, dict]:
    """解析行内表；拒绝重复键、非法覆盖及未闭合输入。"""
    pos += 1
    nested_dict = NestedDict()
    flags = Flags()

    pos = skip_chars(src, pos, TOML_WS)
    if src.startswith("}", pos):
        return pos + 1, nested_dict.dict
    while True:
        pos, key, value = parse_key_value_pair(src, pos, parse_float, nest_lvl)
        key_parent, key_stem = key[:-1], key[-1]
        if flags.is_(key, Flags.FROZEN):
            raise TOMLDecodeError(f"Cannot mutate immutable namespace {key}", src, pos)
        try:
            nest = nested_dict.get_or_create_nest(key_parent, access_lists=False)
        except KeyError:
            raise TOMLDecodeError("Cannot overwrite a value", src, pos) from None
        if key_stem in nest:
            raise TOMLDecodeError(f"Duplicate inline table key {key_stem!r}", src, pos)
        nest[key_stem] = value
        pos = skip_chars(src, pos, TOML_WS)
        c = src[pos : pos + 1]
        if c == "}":
            return pos + 1, nested_dict.dict
        if c != ",":
            raise TOMLDecodeError("Unclosed inline table", src, pos)
        if isinstance(value, (dict, list)):
            flags.set(key, Flags.FROZEN, recursive=True)
        pos += 1
        pos = skip_chars(src, pos, TOML_WS)


def parse_basic_str_escape(
    src: str, pos: Pos, *, multiline: bool = False
) -> tuple[Pos, str]:
    """解析标准转义或 Unicode 转义；多行模式支持反斜线折行。"""
    escape_id = src[pos : pos + 2]
    pos += 2
    if multiline and escape_id in {"\\ ", "\\\t", "\\\n"}:
        # Skip whitespace until next non-whitespace character or end of
        # the doc. Error if non-whitespace is found before newline.
        if escape_id != "\\\n":
            pos = skip_chars(src, pos, TOML_WS)
            try:
                char = src[pos]
            except IndexError:
                return pos, ""
            if char != "\n":
                raise TOMLDecodeError("Unescaped '\\' in a string", src, pos)
            pos += 1
        pos = skip_chars(src, pos, TOML_WS_AND_NEWLINE)
        return pos, ""
    if escape_id == "\\u":
        return parse_hex_char(src, pos, 4)
    if escape_id == "\\U":
        return parse_hex_char(src, pos, 8)
    try:
        return pos, BASIC_STR_ESCAPE_REPLACEMENTS[escape_id]
    except KeyError:
        raise TOMLDecodeError("Unescaped '\\' in a string", src, pos) from None


def parse_basic_str_escape_multiline(src: str, pos: Pos) -> tuple[Pos, str]:
    """以多行模式解析基本字符串转义。"""
    return parse_basic_str_escape(src, pos, multiline=True)


def parse_hex_char(src: str, pos: Pos, hex_len: int) -> tuple[Pos, str]:
    """解析指定长度十六进制 Unicode 标量，拒绝代理区和越界码点。"""
    hex_str = src[pos : pos + hex_len]
    if len(hex_str) != hex_len or not HEXDIGIT_CHARS.issuperset(hex_str):
        raise TOMLDecodeError("Invalid hex value", src, pos)
    pos += hex_len
    hex_int = int(hex_str, 16)
    if not is_unicode_scalar_value(hex_int):
        raise TOMLDecodeError(
            "Escaped character is not a Unicode scalar value", src, pos
        )
    return pos, chr(hex_int)


def parse_literal_str(src: str, pos: Pos) -> tuple[Pos, str]:
    """读取单引号字面量，不解释反斜线转义。"""
    pos += 1  # Skip starting apostrophe
    start_pos = pos
    pos = skip_until(
        src, pos, "'", error_on=ILLEGAL_LITERAL_STR_CHARS, error_on_eof=True
    )
    return pos + 1, src[start_pos:pos]  # Skip ending apostrophe


def parse_multiline_str(src: str, pos: Pos, *, literal: bool) -> tuple[Pos, str]:
    """解析三引号字符串，处理首换行和额外结束引号。"""
    pos += 3
    if src.startswith("\n", pos):
        pos += 1

    if literal:
        delim = "'"
        end_pos = skip_until(
            src,
            pos,
            "'''",
            error_on=ILLEGAL_MULTILINE_LITERAL_STR_CHARS,
            error_on_eof=True,
        )
        result = src[pos:end_pos]
        pos = end_pos + 3
    else:
        delim = '"'
        pos, result = parse_basic_str(src, pos, multiline=True)

    # Add at maximum two extra apostrophes/quotes if the end sequence
    # is 4 or 5 chars long instead of just 3.
    if not src.startswith(delim, pos):
        return pos, result
    pos += 1
    if not src.startswith(delim, pos):
        return pos, result + delim
    pos += 1
    return pos, result + (delim * 2)


def parse_basic_str(src: str, pos: Pos, *, multiline: bool) -> tuple[Pos, str]:
    """解析带转义的基本字符串，按单行或多行规则检查控制字符。"""
    if multiline:
        error_on = ILLEGAL_MULTILINE_BASIC_STR_CHARS
        parse_escapes = parse_basic_str_escape_multiline
    else:
        error_on = ILLEGAL_BASIC_STR_CHARS
        parse_escapes = parse_basic_str_escape
    result = ""
    start_pos = pos
    while True:
        try:
            char = src[pos]
        except IndexError:
            raise TOMLDecodeError("Unterminated string", src, pos) from None
        if char == '"':
            if not multiline:
                return pos + 1, result + src[start_pos:pos]
            if src.startswith('"""', pos):
                return pos + 3, result + src[start_pos:pos]
            pos += 1
            continue
        if char == "\\":
            result += src[start_pos:pos]
            pos, parsed_escape = parse_escapes(src, pos)
            result += parsed_escape
            start_pos = pos
            continue
        if char in error_on:
            raise TOMLDecodeError(f"Illegal character {char!r}", src, pos)
        pos += 1


def parse_value(  # noqa: C901
    src: str, pos: Pos, parse_float: ParseFloat, nest_lvl: int
) -> tuple[Pos, Any]:
    """按 TOML 值类型分派解析并限制行内嵌套深度。"""
    if nest_lvl > MAX_INLINE_NESTING:
        # Pure Python should have raised RecursionError already.
        # This ensures mypyc binaries eventually do the same.
        raise RecursionError(  # pragma: no cover
            "TOML inline arrays/tables are nested more than the allowed"
            f" {MAX_INLINE_NESTING} levels"
        )

    try:
        char: str | None = src[pos]
    except IndexError:
        char = None

    # IMPORTANT: order conditions based on speed of checking and likelihood

    # Basic strings
    if char == '"':
        if src.startswith('"""', pos):
            return parse_multiline_str(src, pos, literal=False)
        return parse_one_line_basic_str(src, pos)

    # Literal strings
    if char == "'":
        if src.startswith("'''", pos):
            return parse_multiline_str(src, pos, literal=True)
        return parse_literal_str(src, pos)

    # Booleans
    if char == "t":
        if src.startswith("true", pos):
            return pos + 4, True
    if char == "f":
        if src.startswith("false", pos):
            return pos + 5, False

    # Arrays
    if char == "[":
        return parse_array(src, pos, parse_float, nest_lvl + 1)

    # Inline tables
    if char == "{":
        return parse_inline_table(src, pos, parse_float, nest_lvl + 1)

    # Dates and times
    datetime_match = RE_DATETIME.match(src, pos)
    if datetime_match:
        try:
            datetime_obj = match_to_datetime(datetime_match)
        except ValueError as e:
            raise TOMLDecodeError("Invalid date or datetime", src, pos) from e
        return datetime_match.end(), datetime_obj
    localtime_match = RE_LOCALTIME.match(src, pos)
    if localtime_match:
        return localtime_match.end(), match_to_localtime(localtime_match)

    # Integers and "normal" floats.
    # The regex will greedily match any type starting with a decimal
    # char, so needs to be located after handling of dates and times.
    number_match = RE_NUMBER.match(src, pos)
    if number_match:
        return number_match.end(), match_to_number(number_match, parse_float)

    # Special floats
    first_three = src[pos : pos + 3]
    if first_three in {"inf", "nan"}:
        return pos + 3, parse_float(first_three)
    first_four = src[pos : pos + 4]
    if first_four in {"-inf", "+inf", "-nan", "+nan"}:
        return pos + 4, parse_float(first_four)

    raise TOMLDecodeError("Invalid value", src, pos)


def is_unicode_scalar_value(codepoint: int) -> bool:
    """判断码点是否在 Unicode 标量范围且不属于代理区。"""
    return (0 <= codepoint <= 55295) or (57344 <= codepoint <= 1114111)


def make_safe_parse_float(parse_float: ParseFloat) -> ParseFloat:
    """包装自定义浮点解析器，禁止返回会与 TOML 容器混淆的 dict/list。

    A decorator to make `parse_float` safe.

    `parse_float` must not return dicts or lists, because these types
    would be mixed with parsed TOML tables and arrays, thus confusing
    the parser. The returned decorated callable raises `ValueError`
    instead of returning illegal types.
    """
    # The default `float` callable never returns illegal types. Optimize it.
    if parse_float is float:
        return float

    def safe_parse_float(float_str: str) -> Any:
        """调用用户浮点解析器并校验返回值不为容器类型。"""
        float_value = parse_float(float_str)
        if isinstance(float_value, (dict, list)):
            raise ValueError("parse_float must not return dicts or lists")
        return float_value

    return safe_parse_float
