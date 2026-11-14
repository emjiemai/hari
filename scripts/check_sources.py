"""Check the source registry and print it:  python -m scripts.check_sources

Reads knowledge/sources/registry.yaml only. Nothing is downloaded.
"""

from __future__ import annotations

import sys

from hari.knowledge.registry import load


def main() -> int:
    try:
        sources = load()
    except ValueError as exc:
        print(f"Registry is not valid: {exc}")
        return 1
    usable = [s for s in sources.values() if s.usable]
    print(f"{len(sources)} sources, {len(usable)} may be loaded as text, "
          f"{len(sources) - len(usable)} are reference only (title and link).")
    for s in sources.values():
        print(f"  {s.source_id:5} {s.status:10} {s.review:10} {s.title}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
