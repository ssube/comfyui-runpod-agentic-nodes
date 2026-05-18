from __future__ import annotations

import argparse
import json

from .runpod_client import RunpodClient


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate the Runpod GraphQL schema used by the CRAG nodes.")
    parser.add_argument("--json", action="store_true", help="Print the full schema check result as JSON.")
    args = parser.parse_args()

    result = RunpodClient().validate_graphql_schema()
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        for type_name, details in result.items():
            status = "ok" if details["present"] and not details["missing"] else "missing"
            missing = f" missing={','.join(details['missing'])}" if details["missing"] else ""
            print(f"{type_name}: {status}{missing}")
    return 1 if any((not details["present"]) or details["missing"] for details in result.values()) else 0


if __name__ == "__main__":
    raise SystemExit(main())
