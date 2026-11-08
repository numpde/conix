"""Failures are classified by operation and evidence, not string parsing."""


class ConfigurationError(Exception):
    """The configured source/schema cannot be used safely."""


class WriterBusy(Exception):
    """Another live session owns the authoritative writer."""


class StateChanged(Exception):
    """Prepared work no longer applies to the durable publication basis."""


class BranchConflict(Exception):
    """Retained history contradicts the source's canonical branch."""


class Blocked(Exception):
    """Durable history was invalidated and requires an explicit repair."""


class CorruptStore(Exception):
    """The stored prefix no longer satisfies its integrity contract."""
