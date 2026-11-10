"""Strict, bounded ABI boundary shared by generic events and read-only calls.

eth-abi owns Solidity binary encoding/decoding; eth-utils owns signatures and
addresses. This module admits supported layouts, checks caller types and
untrusted wire shapes, and preserves indexed hashes without claiming preimages.
It has no database, RPC, filesystem or application-specific dependencies.
"""
from __future__ import annotations

from dataclasses import dataclass
import re

from eth_abi import decode as abi_decode, encode as abi_encode
from eth_abi.grammar import BasicType, TupleType, normalize, parse
from eth_utils import (
    event_signature_to_log_topic, function_signature_to_4byte_selector,
    is_checksum_address, to_checksum_address,
)
from eth_utils.abi import abi_to_signature


class UnsupportedABI(ValueError):
    """The declared Solidity ABI layout is unsupported or ambiguous."""


class InvalidCallerValue(ValueError):
    """A caller supplied a value that does not match its admitted ABI type."""


class MalformedABIData(ValueError):
    """Wire bytes contradict the selected ABI descriptor or canonical encoding."""


class ResourceExceeded(ValueError):
    """A declared type, value or wire payload exceeds the fixed ABI work budget."""


@dataclass(frozen=True, slots=True)
class AbiLimits:
    """Hard ceilings; an admitted catalog may select smaller values."""

    max_bytes: int = 1048576
    max_nesting: int = 8
    max_elements: int = 4096

    def __post_init__(self):
        for (name, ceiling) in (
            ("max_bytes", 1048576),
            ("max_nesting", 8),
            ("max_elements", 4096),
        ):
            value = getattr(self, name)
            if type(value) is not int or not 1 <= value <= ceiling:
                raise ResourceExceeded(f"Invalid ABI limit {name}.")


DEFAULT_LIMITS = AbiLimits()
_NAME = re.compile(r"[A-Za-z_$][A-Za-z0-9_$]*\Z")
_SCALAR = re.compile(r"(?:u?int(?:[0-9]+)?|address|bool|bytes(?:[0-9]+)?|string)\Z")
_ARRAY = re.compile(r"(?:\[(?:[1-9][0-9]*)?\])*\Z")
_ADDRESS = re.compile(r"0x[0-9a-fA-F]{40}\Z")


@dataclass(frozen=True, slots=True)
class ABIValueType:
    """The one admitted recursive type tree, including tuple member names."""

    canonical: str
    kind: str
    dynamic: bool
    width: int | None = None
    item: ABIValueType | None = None
    length: int | None = None
    components: tuple[ABIArgument, ...] = ()


@dataclass(frozen=True, slots=True)
class ABIArgument:
    name: str
    type: ABIValueType
    ordinal: int
    indexed: bool = False
    topic_index: int | None = None


@dataclass(frozen=True, slots=True)
class EventDescriptor:
    name: str
    signature: str
    topic0: bytes
    arguments: tuple[ABIArgument, ...]
    nonindexed: tuple[ABIValueType, ...]

    @property
    def indexed_count(self) -> int:
        return sum(argument.indexed for argument in self.arguments)


@dataclass(frozen=True, slots=True)
class FunctionDescriptor:
    name: str
    signature: str
    selector: bytes
    inputs: tuple[ABIArgument, ...]
    outputs: tuple[ABIArgument, ...]


@dataclass(frozen=True, slots=True)
class IndexedHash:
    """The on-chain topic for an indexed dynamic/array/tuple, NOT its preimage."""

    topic: bytes

    def __post_init__(self):
        if type(self.topic) is not bytes or len(self.topic) != 32:
            raise MalformedABIData("An indexed hash must be one 32-byte topic.")


@dataclass(frozen=True, slots=True)
class ContractDescriptor:
    """Compiled selected events and readable functions for one contract."""

    events: tuple[EventDescriptor, ...]
    functions: tuple[FunctionDescriptor, ...]


def _name(value, *, allow_empty=False):
    if type(value) is not str:
        raise UnsupportedABI("An ABI name must be a string.")
    try:
        name_bytes = value.encode("utf-8")
    except UnicodeError:
        raise UnsupportedABI("An ABI name is not valid UTF-8.") from None
    if len(name_bytes) > 128:
        raise UnsupportedABI("An ABI name exceeds its bound.")
    if not allow_empty and not _NAME.fullmatch(value):
        raise UnsupportedABI("An event/function name is not a Solidity identifier.")
    if "\x00" in value:
        raise UnsupportedABI("An ABI name contains a NUL byte.")
    return value


def _type(entry: dict, limits: AbiLimits, depth: int = 1) -> ABIValueType:
    """Compile one ABI declaration, using eth-abi as the grammar authority.

    Component names are metadata; the library validates their types. The
    recursion and fixed-array bounds protect grammar expansion from hostile
    or cyclic JSON-like objects.
    """
    if depth > limits.max_nesting:
        raise ResourceExceeded("ABI type nesting exceeds the admitted depth.")
    if type(entry) is not dict or type(entry.get("type")) is not str:
        raise UnsupportedABI("ABI argument lacks a type declaration.")

    declared_type = entry["type"]
    if len(declared_type) > 256:
        raise ResourceExceeded("Declared ABI type is too large.")

    base = declared_type.split("[", 1)[0]
    suffix = declared_type[len(base):]
    if len(suffix) > 128 or not _ARRAY.fullmatch(suffix) or not (
        base == "tuple" or _SCALAR.fullmatch(base)
    ):
        raise UnsupportedABI("Unsupported Solidity ABI family or array syntax.")

    dimensions = re.findall(r"\[([0-9]*)\]", suffix)
    if depth + len(dimensions) > limits.max_nesting:
        raise ResourceExceeded("ABI array dimensions exceed the admitted depth.")

    components = ()
    if base == "tuple":
        declared_components = entry.get("components")
        if type(declared_components) is not list or not 0 < len(declared_components) <= 256:
            raise UnsupportedABI("Tuple requires a bounded, nonempty component list.")
        if any(type(component) is not dict for component in declared_components):
            raise UnsupportedABI("Tuple components must be ABI objects.")

        components = tuple(
            ABIArgument(
                name=_name(item.get("name", ""), allow_empty=True),
                type=_type(item, limits, depth + len(dimensions) + 1),
                ordinal=ordinal,
            )
            for ordinal, item in enumerate(declared_components)
        )
        canonical_base = "(" + ",".join(arg.type.canonical for arg in components) + ")"
    else:
        if "components" in entry:
            raise UnsupportedABI("Non-tuple argument has tuple components.")
        canonical_base = base

    try:
        parsed = parse(normalize(canonical_base + suffix))
        parsed.validate()
    except Exception:
        raise UnsupportedABI("ABI type has an invalid Solidity grammar.") from None

    leaf = parsed
    while leaf.is_array:
        leaf = leaf.item_type

    if isinstance(leaf, TupleType) and base == "tuple":
        result = ABIValueType(
            canonical=leaf.to_type_str(),
            kind="tuple",
            dynamic=any(arg.type.dynamic for arg in components),
            components=components,
        )
    elif isinstance(leaf, BasicType) and base != "tuple":
        kind, width = leaf.base, leaf.sub
        if kind in ("uint", "int"):
            if type(width) is not int or width not in range(8, 257, 8):
                raise UnsupportedABI("Unsupported ABI integer width.")
        elif kind == "bytes":
            if width is not None and (
                type(width) is not int or not 1 <= width <= 32
            ):
                raise UnsupportedABI("Unsupported ABI bytesN width.")
        elif kind not in ("address", "bool", "string"):
            raise UnsupportedABI("Unsupported ABI scalar type.")

        result = ABIValueType(
            canonical=leaf.to_type_str(),
            kind=kind,
            dynamic=leaf.is_dynamic,
            width=width,
        )
    else:
        raise UnsupportedABI("ABI grammar and component declaration disagree.")

    if _head_size(result) > limits.max_bytes:
        raise ResourceExceeded("Static ABI shape exceeds maximum encoded size.")

    for dimension in dimensions:
        size = int(dimension) if dimension else None
        if size is not None and not 1 <= size <= limits.max_elements:
            raise ResourceExceeded("Fixed ABI array exceeds the admitted length.")

        result = ABIValueType(
            canonical=result.canonical + "[" + dimension + "]",
            kind="array",
            dynamic=size is None or result.dynamic,
            item=result,
            length=size,
        )
        if _head_size(result) > limits.max_bytes:
            raise ResourceExceeded("Static ABI shape exceeds maximum encoded size.")

    return result


def _head_size(spec):
    if spec.dynamic:
        return 32
    if spec.kind == "array":
        return spec.length * _head_size(spec.item)
    if spec.kind == "tuple":
        return sum(_head_size(arg.type) for arg in spec.components)
    return 32


def _arguments(entries, limits, *, event):
    if type(entries) is not list or len(entries) > 256:
        raise UnsupportedABI("ABI argument list must be bounded.")
    count = 0
    arguments = []
    for ordinal, entry in enumerate(entries):
        if type(entry) is not dict:
            raise UnsupportedABI("ABI argument must be an object.")
        indexed = entry.get("indexed") if event else False
        if event and type(indexed) is not bool:
            raise UnsupportedABI("Event indexed flag must be an explicit boolean.")
        if not event and "indexed" in entry:
            raise UnsupportedABI("A function argument cannot have an indexed flag.")
        count += indexed
        arguments.append(ABIArgument(
            name=_name(entry.get("name", ""), allow_empty=True),
            type=_type(entry, limits),
            ordinal=ordinal,
            indexed=indexed,
            topic_index=count if indexed else None,
        ))
    if count > 3:
        raise UnsupportedABI("Nonanonymous event has more than three indexed arguments.")
    return tuple(arguments)


def compile_event(entry: dict, *, limits: AbiLimits = DEFAULT_LIMITS) -> EventDescriptor:
    """Compile a nonanonymous event with full indexed layout and exact ABI types."""
    if type(entry) is not dict or entry.get("type") != "event" or entry.get("anonymous") is not False:
        raise UnsupportedABI("Only explicit nonanonymous events are supported.")
    name = _name(entry.get("name"))
    arguments = _arguments(entry.get("inputs"), limits, event=True)
    signature = name + "(" + ",".join(arg.type.canonical for arg in arguments) + ")"
    # A canonical compiled signature determines topic0; indexed flags remain
    # part of this descriptor, not the event signature hash.
    topic0 = event_signature_to_log_topic(signature)
    return EventDescriptor(
        name=name, signature=signature, topic0=topic0,
        arguments=arguments,
        nonindexed=tuple(arg.type for arg in arguments if not arg.indexed),
    )


def compile_function(entry: dict, *, limits: AbiLimits = DEFAULT_LIMITS) -> FunctionDescriptor:
    """Compile a view/pure function, including all nested inputs and outputs."""
    if type(entry) is not dict or entry.get("type") != "function":
        raise UnsupportedABI("Expected a function ABI declaration.")
    if entry.get("stateMutability") not in ("view", "pure"):
        raise UnsupportedABI("Only view/pure function declarations are callable.")
    name = _name(entry.get("name"))
    inputs = _arguments(entry.get("inputs"), limits, event=False)
    outputs = _arguments(entry.get("outputs"), limits, event=False)
    signature = name + "(" + ",".join(arg.type.canonical for arg in inputs) + ")"
    return FunctionDescriptor(
        name=name, signature=signature,
        selector=function_signature_to_4byte_selector(signature),
        inputs=inputs, outputs=outputs,
    )


def compile_contract(abi: list, selected_events: tuple[str, ...], *,
                     limits: AbiLimits = DEFAULT_LIMITS) -> ContractDescriptor:
    """Compile only selected events and all readable functions from one ABI."""
    if type(abi) is not list or not 0 < len(abi) <= 256:
        raise UnsupportedABI("Contract ABI must be a bounded nonempty list.")
    if type(selected_events) is not tuple or not selected_events or any(
        type(signature) is not str for signature in selected_events
    ) or len(set(selected_events)) != len(selected_events):
        raise UnsupportedABI("Selected events must have unique canonical signatures.")

    events, functions = {}, {}
    for entry in abi:
        if type(entry) is not dict:
            raise UnsupportedABI("ABI entry is not an object.")
        kind = entry.get("type")
        if kind == "event":
            try:
                signature = abi_to_signature(entry)
            except (ValueError, TypeError, KeyError):
                raise UnsupportedABI("Malformed event declaration.") from None
            if signature not in selected_events:
                continue
            event = compile_event(entry, limits=limits)
            if signature in events:
                raise UnsupportedABI("Duplicate or ambiguous selected event ABI.")
            events[signature] = event
        elif kind == "function" and entry.get("stateMutability") in ("view", "pure"):
            function = compile_function(entry, limits=limits)
            if function.signature in functions or any(
                old.selector == function.selector for old in functions.values()
            ):
                raise UnsupportedABI("Duplicate or ambiguous read-only function ABI.")
            functions[function.signature] = function

    if set(events) != set(selected_events):
        raise UnsupportedABI("Some selected event signatures are absent from the ABI.")
    if len({event.topic0 for event in events.values()}) != len(events):
        raise UnsupportedABI("Selected events have conflicting topic0 identities.")
    return ContractDescriptor(events=tuple(events.values()), functions=tuple(functions.values()))


def _word(data, offset):
    if offset < 0 or offset + 32 > len(data):
        raise MalformedABIData("ABI payload contains an out-of-range word.")
    return int.from_bytes(data[offset:offset + 32], "big")


def _check_sequence(types, data, start, limits, budget):
    """Bound nested array lengths and offset targets before eth-abi decoding."""
    types = tuple(types)
    head_bytes = sum(_head_size(spec) for spec in types)
    if start < 0 or start + head_bytes > len(data):
        raise MalformedABIData("ABI tuple/array head is truncated.")
    current = start
    for spec in types:
        if spec.dynamic:
            offset = _word(data, current)
            if offset % 32 or offset < head_bytes or start + offset >= len(data):
                raise MalformedABIData("ABI dynamic offset is invalid.")
            _check_value(spec, data, start + offset, limits, budget)
            current += 32
        else:
            _check_value(spec, data, current, limits, budget)
            current += _head_size(spec)


def _check_value(spec, data, start, limits, budget):
    if spec.kind == "array":
        length = _word(data, start) if spec.length is None else spec.length
        element_start = start + (32 if spec.length is None else 0)
        if length > limits.max_elements:
            raise ResourceExceeded("Encoded ABI array exceeds its element cap.")
        budget[0] += length
        if budget[0] > limits.max_elements * 4:
            raise ResourceExceeded("Total ABI array elements exceed a bounded work budget.")
        if length * _head_size(spec.item) > len(data) - element_start:
            raise MalformedABIData("ABI array claims more elements than its payload contains.")
        _check_sequence((spec.item,) * length, data, element_start, limits, budget)
    elif spec.kind == "tuple":
        _check_sequence((arg.type for arg in spec.components), data, start, limits, budget)
    elif spec.kind in ("bytes", "string") and spec.width is None:
        length = _word(data, start)
        if length > limits.max_bytes or start + 32 + ((length + 31) // 32) * 32 > len(data):
            raise ResourceExceeded("Encoded ABI dynamic data exceeds its byte bounds.")
    else:
        _word(data, start)


def _decode(types, data, limits):
    if type(data) is not bytes:
        raise MalformedABIData("ABI wire input must be bytes.")
    if len(data) > limits.max_bytes:
        raise ResourceExceeded("ABI payload exceeds the byte budget.")
    if len(data) % 32:
        raise MalformedABIData("ABI payload is not word-aligned.")

    _check_sequence(types, data, 0, limits, [0])
    canonical = tuple(spec.canonical for spec in types)
    try:
        decoded = abi_decode(canonical, data, strict=True)
        if abi_encode(canonical, decoded) != data:
            raise MalformedABIData("ABI payload has noncanonical padding, offsets or trailing bytes.")
    except MalformedABIData:
        raise
    except Exception:
        raise MalformedABIData("ABI decoding or canonicality verification failed.") from None
    return tuple(_present(spec, value) for spec, value in zip(types, decoded, strict=True))


def _present(spec, value):
    if spec.kind == "address":
        return to_checksum_address(value)
    if spec.kind == "tuple":
        return tuple(_present(arg.type, item) for arg, item in zip(spec.components, value, strict=True))
    if spec.kind == "array":
        return tuple(_present(spec.item, item) for item in value)
    return value


def decode_event(descriptor: EventDescriptor, topics: tuple[bytes, ...], data: bytes, *,
                 limits: AbiLimits = DEFAULT_LIMITS) -> tuple:
    """Decode an exact event layout; never conflate an indexed hash with text."""
    if type(descriptor) is not EventDescriptor:
        raise UnsupportedABI("Expected a compiled event descriptor.")
    if type(topics) is not tuple or len(topics) != 1 + descriptor.indexed_count or any(
        type(topic) is not bytes or len(topic) != 32 for topic in topics
    ):
        raise MalformedABIData("Event has an incorrect number or size of topics.")
    if topics[0] != descriptor.topic0:
        raise MalformedABIData("Event topic0 differs from the selected signature.")

    nonindexed = iter(_decode(descriptor.nonindexed, data, limits))
    values = []
    for arg in descriptor.arguments:
        if not arg.indexed:
            values.append(next(nonindexed))
        elif arg.type.dynamic or arg.type.kind in ("tuple", "array"):
            values.append(IndexedHash(topics[arg.topic_index]))
        else:
            values.append(_decode((arg.type,), topics[arg.topic_index], limits)[0])
    return tuple(values)


def _caller(spec, value, limits, budget):
    if spec.kind in ("uint", "int"):
        if type(value) is not int:
            raise InvalidCallerValue("ABI integer requires an exact Python int, not bool/float.")
        # eth-abi enforces the declared signedness and width during encoding.
    elif spec.kind == "bool":
        if type(value) is not bool:
            raise InvalidCallerValue("ABI bool requires a Python bool.")
    elif spec.kind == "address":
        if type(value) is not str or not _ADDRESS.fullmatch(value) or (
            any(char.isupper() for char in value[2:])
            and any(char.islower() for char in value[2:])
            and not is_checksum_address(value)
        ):
            raise InvalidCallerValue("ABI address must be 20 hex bytes with a valid mixed-case checksum.")
        value = to_checksum_address(value)
    elif spec.kind == "bytes":
        if type(value) is not bytes or (spec.width is not None and len(value) != spec.width):
            raise InvalidCallerValue("ABI bytes require exact bytes and fixed-width length.")
        if len(value) > limits.max_bytes:
            raise ResourceExceeded("ABI bytes exceed the byte budget.")
    elif spec.kind == "string":
        if type(value) is not str:
            raise InvalidCallerValue("ABI string requires a Python str.")
        try:
            length = len(value.encode("utf-8"))
        except UnicodeError:
            raise InvalidCallerValue("ABI string must be valid UTF-8.") from None
        if length > limits.max_bytes:
            raise ResourceExceeded("ABI string exceeds the byte budget.")
    elif spec.kind == "tuple":
        if type(value) is not tuple or len(value) != len(spec.components):
            raise InvalidCallerValue("ABI tuple has the wrong Python shape.")
        value = tuple(_caller(arg.type, item, limits, budget) for arg, item in zip(spec.components, value))
    elif spec.kind == "array":
        if type(value) is not tuple or len(value) > limits.max_elements or (
            spec.length is not None and len(value) != spec.length
        ):
            raise InvalidCallerValue("ABI array has the wrong Python shape or length.")
        budget[0] += len(value)
        if budget[0] > limits.max_elements * 4:
            raise ResourceExceeded("Total ABI array elements exceed a bounded work budget.")
        value = tuple(_caller(spec.item, item, limits, budget) for item in value)
    else:
        raise UnsupportedABI("Unrecognized compiled ABI type.")
    return value


def encode_indexed_filter(descriptor: EventDescriptor, field_name: str, value, *,
                          limits: AbiLimits = DEFAULT_LIMITS) -> bytes:
    """Encode one uniquely named, indexed, static scalar equality predicate."""
    if type(descriptor) is not EventDescriptor or type(field_name) is not str or not field_name:
        raise InvalidCallerValue("Indexed filter requires a named event field.")
    matches = [arg for arg in descriptor.arguments if arg.name == field_name]
    if len(matches) != 1 or not matches[0].indexed:
        raise InvalidCallerValue("Filter field is missing, ambiguous or nonindexed.")
    argument = matches[0]
    if argument.type.dynamic or argument.type.kind in ("tuple", "array"):
        raise InvalidCallerValue("Indexed hash cannot be filtered as an ABI preimage.")
    typed = _caller(argument.type, value, limits, [0])
    try:
        encoded = abi_encode((argument.type.canonical,), (typed,))
    except Exception:
        raise InvalidCallerValue("Cannot encode the requested indexed scalar.") from None
    if len(encoded) != 32:
        raise UnsupportedABI("Indexed scalar did not encode to one topic.")
    return encoded


def encode_call(descriptor: FunctionDescriptor, arguments: tuple, *,
                limits: AbiLimits = DEFAULT_LIMITS) -> bytes:
    """Encode a declared read-only function call, never a transaction."""
    if type(descriptor) is not FunctionDescriptor or type(arguments) is not tuple or (
        len(arguments) != len(descriptor.inputs)
    ):
        raise InvalidCallerValue("Function call argument count/shape is invalid.")
    types = tuple(arg.type.canonical for arg in descriptor.inputs)
    budget = [0]
    values = tuple(
        _caller(arg.type, value, limits, budget) for arg, value in zip(descriptor.inputs, arguments)
    )
    try:
        encoded = abi_encode(types, values)
    except Exception:
        raise InvalidCallerValue("Cannot ABI-encode a read-only function call.") from None
    if len(encoded) > limits.max_bytes:
        raise ResourceExceeded("Encoded function arguments exceed the byte budget.")
    return descriptor.selector + encoded


def decode_return(descriptor: FunctionDescriptor, data: bytes, *,
                  limits: AbiLimits = DEFAULT_LIMITS) -> tuple:
    """Return an outer tuple, even when one output is itself a tuple."""
    if type(descriptor) is not FunctionDescriptor:
        raise UnsupportedABI("Expected a compiled read-only function descriptor.")
    return _decode(tuple(arg.type for arg in descriptor.outputs), data, limits)
