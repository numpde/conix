"""One-shot owner-only schema initialization; never run in the indexer service.

The owner DSN arrives only through bounded stdin in a confined container.
It is never mounted, put in argv, recorded in an image, or printed.
"""

import asyncio
import sys

from conix.input_staging import InputError, MAX_DSN, _credential_text, load_runtime_inputs
from conix.schema import SchemaError, initialize


def main():
    if sys.argv[1:] != ["initialize"]:
        raise SystemExit("Use python -I -m conix.schema_cli initialize.")
    try:
        owner = _credential_text(sys.stdin.buffer.read(MAX_DSN + 1))
        catalog, _runtime_dsn = load_runtime_inputs()
        asyncio.run(initialize(owner, catalog))
    except (InputError, SchemaError) as error:
        raise SystemExit(
            f"Conix owner initialization refused ({type(error).__name__}); "
            "inspect the selected schema and owner privilege."
        ) from None
    except Exception as error:
        raise SystemExit(
            f"Conix owner initialization failed ({type(error).__name__}); "
            "inspect durable database state before retrying."
        ) from None

    print("Conix owner-only schema initialization completed.", flush=True)


if __name__ == "__main__":
    main()
