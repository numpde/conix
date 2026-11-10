"""Admit only the verified dapp32 PayToPlay fixture-v1 JSON contract.

No account keys are ever included in diagnostic exceptions or process output.
The descriptor establishes deployment facts; Docker network inspection and
real RPC checks independently qualify their corresponding live identities.
"""
import json
from pathlib import Path
import re
import traceback

LIMIT = 1048576
IDENTITY = re.compile(r"[a-z0-9][a-z0-9_-]{0,47}\Z")
ADDRESS = re.compile(r"0x[0-9a-fA-F]{40}\Z")
KEY = re.compile(r"0x[0-9a-fA-F]{64}\Z")
REQUIRED_ACCOUNTS = frozenset({"admin", "customer", "relayer", "payee"})
REQUIRED_CONTRACTS = frozenset({"CamRoot", "PayToPlay", "PayToPlayUI"})
EVENT = [
    ("customer", "address", True),
    ("channelId", "uint256", True),
    ("payee", "address", True),
    ("amount", "uint256", False),
    ("voucherPaid", "uint256", False),
    ("invoiceHash", "bytes32", False),
]
FUNCTIONS = {
    "createChannel": ("address", "address", "uint64"),
    "setPayee": ("uint256", "address"),
    "registerVoucherKey": ("uint256", "address"),
    "fund": ("uint256", "uint256"),
    "collect": ("address", "uint256", "uint256", "bytes32", "uint256", "bytes"),
    "voucherDigest": ("address", "uint256", "uint256", "bytes32"),
    "channelCount": (),
    "viewPosition": ("address", "uint256", "address"),
}


class FixtureError(ValueError):
    """A descriptor lacks the precise source, identity or ABI contract."""


def _pairs(rows):
    """Reject duplicate JSON keys before accepting any source identity."""
    result = {}
    for (name, value) in rows:
        if name in result:
            raise FixtureError("Fixture JSON contains duplicate object members.")

        result[name] = value
    return result


def _bad_constant(_value):
    raise FixtureError("Fixture JSON contains a nonfinite number.")


def _address(value):
    return type(value) is str and ADDRESS.fullmatch(value) is not None


def _uint(value, maximum=2**63 - 1):
    return type(value) is int and 0 <= value <= maximum


def decode(raw: bytes, *, fixture_id: str | None):
    """Admit one strict descriptor, optionally checking a producer-selected ID."""
    if fixture_id is not None and (
        type(fixture_id) is not str or IDENTITY.fullmatch(fixture_id) is None
    ):
        raise FixtureError("Fixture identity must use the dapp32 supported lowercase name format.")

    if type(raw) is not bytes or not 0 < len(raw) <= LIMIT or len(raw.splitlines()) != 1:
        raise FixtureError("Fixture descriptor must be exactly one bounded UTF-8 JSON line.")

    try:
        document = json.loads(
            raw.decode("utf-8"), object_pairs_hook=_pairs,
            parse_constant=_bad_constant,
        )
    except (UnicodeError, ValueError, RecursionError) as error:
        raise FixtureError("Fixture descriptor is not well-formed unique-key UTF-8 JSON.") from None

    if type(document) is not dict or set(document) != {
        "schema", "fixtureId", "composeProject", "dockerNetwork",
        "rpcUrl", "chainId", "contracts", "publicFixtureAccounts",
    }:
        raise FixtureError("Fixture descriptor does not match the exact v1 envelope.")
    if fixture_id is None:
        # A borrowed fixture selects its own exact ID. A newly created
        # fixture must instead match the caller's independently chosen ID.
        fixture_id = document["fixtureId"]
        if type(fixture_id) is not str or IDENTITY.fullmatch(fixture_id) is None:
            raise FixtureError("Borrowed fixture ID is not a supported identity.")

    project = "dapps-pay-to-play-fixture-" + fixture_id
    network = project + "_pay-to-play_local"
    if (
        document["schema"] != "pay-to-play.local-fixture.v1"
        or document["fixtureId"] != fixture_id
        or document["composeProject"] != project
        or document["dockerNetwork"] != network
        or document["rpcUrl"] != "http://pay-to-play-anvil:8545"
        or type(document["chainId"]) is not int or document["chainId"] != 31337
    ):
        raise FixtureError("Fixture source identity, network or RPC endpoint differs from the selected fixture.")

    # Names and ABI alone are not proof of a live deployment; the campaign
    # separately checks the producer network and actual RPC instance.
    contracts = document["contracts"]
    if type(contracts) is not dict or set(contracts) != REQUIRED_CONTRACTS:
        raise FixtureError("Fixture must declare exactly the verified base deployment contracts.")

    for (name, item) in contracts.items():
        required = {"address", "deploymentBlock", "creationTransaction"}
        if name == "PayToPlay":
            required.add("abi")
        if (type(item) is not dict or set(item) != required or not _address(item["address"])
            or not _uint(item["deploymentBlock"])
            or type(item["creationTransaction"]) is not str
            or KEY.fullmatch(item["creationTransaction"]) is None):
            raise FixtureError("Fixture deployment coordinates or creation transaction are malformed.")
    pay_to_play = contracts["PayToPlay"]
    abi = pay_to_play["abi"]
    if type(abi) is not list or not 0 < len(abi) <= 256 or any(type(row) is not dict for row in abi):
        raise FixtureError("Fixture PayToPlay ABI must be a bounded list of entries.")

    event = [
        row for row in abi if row.get("type") == "event" and row.get("name") == "VoucherCollected"
    ]
    if len(event) != 1 or event[0].get("anonymous") is not False or [
        (item.get("name"), item.get("type"), item.get("indexed"))
        for item in event[0].get("inputs", [])
    ] != EVENT:
        raise FixtureError("Fixture VoucherCollected ABI does not match the indexed contract.")

    for (name, types) in FUNCTIONS.items():
        matches = [
            row for row in abi if row.get("type") == "function" and row.get("name") == name
            and tuple(argument.get("type") for argument in row.get("inputs", ())) == types
        ]
        if len(matches) != 1:
            raise FixtureError("Fixture PayToPlay method signatures differ from the tested deployment.")

    accounts = document["publicFixtureAccounts"]
    if type(accounts) is not dict or set(accounts) != REQUIRED_ACCOUNTS:
        raise FixtureError("Fixture must provide exactly the four public test accounts.")

    for item in accounts.values():
        if (type(item) is not dict or set(item) != {"address", "privateKey"}
            or not _address(item["address"]) or type(item["privateKey"]) is not str
            or KEY.fullmatch(item["privateKey"]) is None):
            raise FixtureError("Fixture test-account coordinates are malformed.")

    if len({value["address"].lower() for value in accounts.values()}) != 4:
        raise FixtureError("Fixture test-account addresses must be distinct.")

    return document


# A deliberately narrow wire diagnostic: tests can reveal their own fixed,
# nonsecret assertion messages, but never an upstream exception's raw value.
# The campaign prints this verified record only after it has attempted cleanup.
_FAILURE_PREFIX = b"CONIX_PHASE_FAILURE "
_PHASES = frozenset({"preflight", "prepare", "initialize", "baseline", "exercise", "resume", "reorg"})
_PUBLIC_TEXT = re.compile(r"[A-Za-z0-9][A-Za-z0-9 _.,:;()/'=_-]{0,239}\Z")
_CATEGORY = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,63}\Z")
_PRIVATE_MARKERS = ("privatekey", "postgresql:", "password", "secret", "://")


class ScenarioFailure(AssertionError):
    """An explicit, source-controlled scenario assertion with a safe message."""


def _public_reason(value):
    """Allow only bounded diagnostics without credential-shaped content."""
    return (
        type(value) is str and _PUBLIC_TEXT.fullmatch(value) is not None
        and not any(marker in value.lower() for marker in _PRIVATE_MARKERS)
        and re.search(r"(?:0x)?[a-fA-F0-9]{64}", value) is None
    )


def phase_failure_record(phase: str, error: Exception) -> bytes:
    """A safe diagnostic for a private fixture's stderr, not a traceback dump.

    Only our explicit assertion/descriptor classes may contribute their text.
    Unexpected exceptions expose their type and source location, never their
    potentially credential-bearing arguments or upstream RPC response.
    """
    if phase not in _PHASES:
        raise ValueError("Unknown fixture phase.")

    category = type(error).__name__
    if type(error) in (ScenarioFailure, FixtureError) and _public_reason(str(error)):
        reason = str(error)
        # Conix-owned cleanup notes are nonsecret phase evidence. Preserve
        # one when it fits the same bounded public record.
        for note in getattr(error, "__notes__", ()):
            described = f"{reason} {note}"
            if _public_reason(described):
                reason = described
                break
    elif isinstance(error, TimeoutError):
        reason = "Operation deadline exceeded."
    else:
        frames = traceback.extract_tb(error.__traceback__)
        origin = frames[-1] if frames else None
        location = (
            f"{Path(origin.filename).name}:{origin.lineno} in {origin.name}"
            if origin else "the current phase"
        )
        reason = f"Unexpected error at {location}; inspect the selected operation."
        if not _public_reason(reason):
            reason = "Unexpected error; inspect the selected operation."
    return _FAILURE_PREFIX + json.dumps(
        {"phase": phase, "category": category, "reason": reason},
        separators=(",", ":"), ensure_ascii=True,
    ).encode("ascii")


def parse_phase_failure(stderr: bytes, *, phase: str) -> str | None:
    """Admit only the bounded, validated failure record for this exact phase."""
    if phase not in _PHASES or type(stderr) is not bytes:
        return None

    for line in reversed(stderr[-16384:].splitlines()):
        if not line.startswith(_FAILURE_PREFIX):
            continue

        try:
            record = json.loads(line[len(_FAILURE_PREFIX):])
        except (ValueError, UnicodeError):
            continue
        if (
            type(record) is dict
            and set(record) == {"phase", "category", "reason"}
            and record["phase"] == phase
            and type(record["category"]) is str
            and _CATEGORY.fullmatch(record["category"]) is not None
            and _public_reason(record["reason"])
        ):
            return f"{record['category']}: {record['reason']}"

    return None


def acceptance_preflight(raw: bytes, *, chain_id: int) -> dict:
    """Admit only chain identity and observed genesis; never private fixture keys."""
    if type(raw) is not bytes or not 0 < len(raw) <= 512 or len(raw.splitlines()) != 1:
        raise FixtureError("Expected one bounded preflight JSON line.")
    try:
        result = json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs, parse_constant=_bad_constant)
    except (UnicodeError, ValueError, RecursionError):
        raise FixtureError("Preflight result is invalid JSON.") from None
    if (
        type(result) is not dict or set(result) != {"schema", "chainId", "genesisHash"}
        or result["schema"] != "conix.acceptance.preflight.v1"
        or type(result["chainId"]) is not int or result["chainId"] != chain_id
        or type(result["genesisHash"]) is not str
        or KEY.fullmatch(result["genesisHash"]) is None
    ):
        raise FixtureError("Preflight has an invalid source identity or extra fields.")
    return result


def public_acceptance_source(descriptor: dict) -> dict:
    """Project a validated private fixture onto owner-safe chain coordinates.

    The ABI used by acceptance is a reviewed, installed JSON fixture. Wallet
    accounts, producer identity, private keys and arbitrary ABI fields never
    cross into the schema owner's stdin.
    """
    deployed = descriptor["contracts"]["PayToPlay"]
    return {
        "schema": "conix.acceptance.public-source.v1",
        "chainId": descriptor["chainId"],
        "rpcUrl": descriptor["rpcUrl"],
        "payment": {
            "address": deployed["address"],
            "deploymentBlock": deployed["deploymentBlock"],
        },
    }


def admit_public_acceptance_source(value: dict) -> dict:
    """Validate the minimal public source admitted by the owner phase."""
    payment = value.get("payment") if type(value) is dict else None
    if (
        type(value) is not dict
        or set(value) != {"schema", "chainId", "rpcUrl", "payment"}
        or value["schema"] != "conix.acceptance.public-source.v1"
        or type(value["chainId"]) is not int or value["chainId"] != 31337
        or value["rpcUrl"] != "http://pay-to-play-anvil:8545"
        or type(payment) is not dict or set(payment) != {"address", "deploymentBlock"}
        or not _address(payment["address"])
        or not _uint(payment["deploymentBlock"])
    ):
        raise FixtureError("Owner phase requires exact public source metadata.")
    return value



_TEST_VIEW_NAMES = frozenset({"channelCount", "voucherDigest", "viewPosition"})
_PUBLIC_ABI_NAME = re.compile(r"[A-Za-z_$][A-Za-z0-9_$]{0,63}\Z")
_PUBLIC_ABI_TYPE = re.compile(
    r"(?:address|bool|string|bytes(?:[0-9]{1,2})?|u?int(?:[0-9]{1,3})?|tuple)"
    r"(?:\[[0-9]*\])*\Z"
)



def _public_abi_argument(argument: dict) -> dict:
    """Project bounded Solidity ABI names/types, never arbitrary producer data."""
    if type(argument) is not dict:
        raise FixtureError("Selected fixture view contains a malformed ABI argument.")

    name = argument.get("name", "")
    abi_type = argument.get("type")
    if (
        type(name) is not str
        or (name and _PUBLIC_ABI_NAME.fullmatch(name) is None)
        or type(abi_type) is not str
        or _PUBLIC_ABI_TYPE.fullmatch(abi_type) is None
    ):
        raise FixtureError("Selected fixture view contains an invalid ABI name or type.")

    projected = {"name": name, "type": abi_type}
    if "components" in argument:
        components = argument["components"]
        if type(components) is not list or not abi_type.startswith("tuple"):
            raise FixtureError("Only tuple ABI values may declare components.")
        projected["components"] = [
            _public_abi_argument(component) for component in components
        ]

    return projected


def public_test_source(descriptor: dict) -> dict:
    """Give test consumers public contract/account facts, never wallet keys.

    The host has already admitted the complete private dapp32 descriptor.
    No test phase signs with its fixture keys: transactions use Anvil test
    impersonation and typed-data signing uses a separate unlocked test account.
    """
    payment = descriptor["contracts"]["PayToPlay"]
    return {
        "schema": "conix.acceptance.public-test-source.v1",
        "fixtureId": descriptor["fixtureId"],
        "chainId": descriptor["chainId"],
        "rpcUrl": descriptor["rpcUrl"],
        "contracts": {
            "PayToPlay": {
                "address": payment["address"],
                "deploymentBlock": payment["deploymentBlock"],
                # Only these deployed views are used to establish the test
                # oracle. Do not forward arbitrary ABI metadata or events.
                "abi": [
                    {
                        "type": "function",
                        "name": entry["name"],
                        "inputs": [
                            _public_abi_argument(argument)
                            for argument in entry["inputs"]
                        ],
                        "outputs": [
                            _public_abi_argument(argument)
                            for argument in entry.get("outputs", [])
                        ],
                    }
                    for entry in payment["abi"]
                    if entry.get("type") == "function"
                    and entry.get("name") in _TEST_VIEW_NAMES
                ],
            },
        },
        "publicFixtureAccounts": {
            role: {"address": account["address"]}
            for (role, account) in descriptor["publicFixtureAccounts"].items()
        },
    }


def admit_public_test_source(source: dict, *, fixture_id: str) -> dict:
    """Admit the one keyless source projected for installed test consumers."""
    contracts = source.get("contracts") if type(source) is dict else None
    payment = contracts.get("PayToPlay") if type(contracts) is dict else None
    accounts = source.get("publicFixtureAccounts") if type(source) is dict else None

    if (
        type(source) is not dict
        or set(source) != {
            "schema", "fixtureId", "chainId", "rpcUrl", "contracts", "publicFixtureAccounts",
        }
        or source["schema"] != "conix.acceptance.public-test-source.v1"
        or source["fixtureId"] != fixture_id
        or type(source["chainId"]) is not int or source["chainId"] != 31337
        or source["rpcUrl"] != "http://pay-to-play-anvil:8545"
        or type(contracts) is not dict or set(contracts) != {"PayToPlay"}
        or type(payment) is not dict
        or set(payment) != {"address", "deploymentBlock", "abi"}
        or not _address(payment["address"])
        or not _uint(payment["deploymentBlock"])
        or type(payment["abi"]) is not list
        or not 0 < len(payment["abi"]) <= 256
        or any(type(entry) is not dict for entry in payment["abi"])
        or type(accounts) is not dict or set(accounts) != REQUIRED_ACCOUNTS
        or any(type(account) is not dict or set(account) != {"address"}
               or not _address(account["address"]) for account in accounts.values())
        or len({account["address"].lower() for account in accounts.values()}) != 4
    ):
        raise FixtureError("The consumer source does not match the selected keyless fixture.")

    return source


def acceptance_payments(raw: bytes) -> dict:
    """Admit only the public test receipt passed to a fresh process.

    The test process may hold fixture wallet keys; its stdout must instead
    carry exclusively source/checkpoint and transaction facts.
    """
    if type(raw) is not bytes or not 0 < len(raw) <= 4096 or len(raw.splitlines()) != 1:
        raise FixtureError("Expected one bounded payment acceptance receipt.")

    try:
        record = json.loads(
            raw.decode("utf-8"), object_pairs_hook=_pairs, parse_constant=_bad_constant,
        )
    except (ValueError, UnicodeError, RecursionError):
        raise FixtureError("Payment acceptance receipt is not strict JSON.") from None

    return admit_payments(record)


def admit_payments(record: dict) -> dict:
    """One shared public checkpoint/receipt contract across processes."""
    if type(record) is not dict or set(record) != {
        "schema", "checkpoint", "transactions", "collected",
    } or record["schema"] != "conix.acceptance.payments.v1":
        raise FixtureError("Payment acceptance receipt has an invalid envelope.")

    checkpoint = record["checkpoint"]
    block = checkpoint.get("block") if type(checkpoint) is dict else None
    hex64 = re.compile(r"[0-9a-f]{64}\Z")
    if (
        type(checkpoint) is not dict
        or set(checkpoint) != {"source", "generation", "start", "revision", "block"}
        or type(checkpoint["source"]) is not str
        or hex64.fullmatch(checkpoint["source"]) is None
        or type(checkpoint["generation"]) is not str
        or re.fullmatch(r"[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}", checkpoint["generation"]) is None
        or not _uint(checkpoint["start"])
        or not _uint(checkpoint["revision"])
        or type(block) is not dict
        or set(block) != {"number", "hash", "parent", "timestamp"}
        or not _uint(block["number"]) or not _uint(block["timestamp"])
        or block["number"] < checkpoint["start"]
        or any(type(block[field]) is not str or hex64.fullmatch(block[field]) is None
               for field in ("hash", "parent"))
    ):
        raise FixtureError("Payment acceptance receipt has an invalid checkpoint.")

    if (
        type(record["transactions"]) is not list
        or not 1 <= len(record["transactions"]) <= 32
        or any(type(tx) is not str or KEY.fullmatch(tx) is None
               for tx in record["transactions"])
        or len(set(record["transactions"])) != len(record["transactions"])
        or type(record["collected"]) is not list
        or not 1 <= len(record["collected"]) <= 32
        or any(not _uint(value, maximum=(1 << 256) - 1) for value in record["collected"])
    ):
        raise FixtureError("Payment acceptance receipt has invalid event facts.")

    return record


def admit_tokens(record: dict | None) -> dict | None:
    """Admit only the public token deployment identities, never account keys.

    One public token manifest binds the owner phase, the writer, all readers
    and the independently verified mined creation receipts to the same source.
    """
    if record is None:
        return None

    if type(record) is not dict or set(record) != {"schema", "token20", "token721"}:
        raise FixtureError("Selected token fixture has an unexpected public envelope.")
    if record["schema"] != "conix.acceptance.tokens.v1":
        raise FixtureError("Selected token fixture has an unsupported schema.")

    for name in ("token20", "token721"):
        selected = record[name]
        if (
            type(selected) is not dict
            or set(selected) != {"address", "deploymentBlock", "creationTransaction"}
            or not _address(selected["address"])
            or not _uint(selected["deploymentBlock"])
            or not isinstance(selected["creationTransaction"], str)
            or KEY.fullmatch(selected["creationTransaction"]) is None
        ):
            raise FixtureError("Selected token fixture lacks valid deployment coordinates.")

    if record["token20"]["address"].lower() == record["token721"]["address"].lower():
        raise FixtureError("Selected token fixtures reuse one emitting contract address.")

    return record


def acceptance_tokens(raw: bytes) -> dict:
    """Read one successful deployment receipt from the disposable Anvil process."""
    if type(raw) is not bytes or not 0 < len(raw) <= 4096 or len(raw.splitlines()) != 1:
        raise FixtureError("Expected one bounded token deployment receipt.")

    try:
        record = json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs, parse_constant=_bad_constant)
    except (UnicodeError, ValueError, RecursionError):
        raise FixtureError("Token deployment receipt is not strict JSON.") from None

    return admit_tokens(record)
