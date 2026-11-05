"""Entry point for the `obi` command. Commands arrive in Phase 0 step 5."""

import sys


def main() -> int:
    print("obi: no commands yet (validate/render land in Phase 0, step 5)", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
