import argparse
import json
from pathlib import Path
from .core import audit


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--input", required=True)
    p.add_argument("--output", required=True)
    a = p.parse_args()
    try:
        result = audit(json.loads(Path(a.input).read_text()))
        target = Path(a.output)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("x") as f:
            json.dump(result, f, indent=2)
            f.write("\n")
        print(json.dumps({"loans": len(result["loans"]), "overdue": result["overdue"], "exceptions": result["exceptions"]}, sort_keys=True))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        p.exit(2, f"loan-audit: {exc}\n")


if __name__ == "__main__":
    main()
