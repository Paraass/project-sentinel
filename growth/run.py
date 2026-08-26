from pathlib import Path
from growth_machine.core import run

ROOT = Path(__file__).parent

if __name__ == "__main__":
    print("Run 1")
    print(run(ROOT / "data" / "targets_run1.csv", ROOT / "examples" / "run1"))
    print("Run 2")
    print(run(ROOT / "data" / "targets_run2.csv", ROOT / "examples" / "run2"))
