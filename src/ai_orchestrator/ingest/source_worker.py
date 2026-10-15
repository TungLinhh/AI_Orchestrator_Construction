"""Offline parser process. Resource limits apply before opening untrusted input."""

import json
import resource
import sys


def main() -> None:
    resource.setrlimit(resource.RLIMIT_CPU, (25, 25))
    resource.setrlimit(resource.RLIMIT_AS, (1024 * 1024 * 1024, 1024 * 1024 * 1024))
    resource.setrlimit(resource.RLIMIT_FSIZE, (32 * 1024 * 1024, 32 * 1024 * 1024))
    from ai_orchestrator.ingest.source_files import MAX_BYTES, extract

    try:
        data = sys.stdin.buffer.read(MAX_BYTES + 1)
        result = extract(sys.argv[1], data)
    except Exception as exc:
        result = {
            "parser": "source-v1",
            "state": "failed",
            "segments": [],
            "tables": [],
            "warnings": [],
            "error": str(exc)[:600],
        }
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
