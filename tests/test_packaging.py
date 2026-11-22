"""The built wheel must contain every package and resource the app imports.

CI and these tests run from the source tree (editable install), so a package missing from the
wheel would only show up in the snap: that happened once with super8.ai. Build the real wheel and
check it against the source tree."""
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_wheel_contains_every_package_and_resource(tmp_path):
    subprocess.run([sys.executable, "-m", "pip", "wheel", "--no-deps", "--no-build-isolation", "-q",
                    str(ROOT), "-w", str(tmp_path)], check=True, cwd=tmp_path)
    wheel = next(tmp_path.glob("super8-*.whl"))
    names = set(zipfile.ZipFile(wheel).namelist())
    source_modules = {str(p.relative_to(ROOT)) for p in (ROOT / "super8").rglob("*.py")
                      if "__pycache__" not in p.parts}
    missing = sorted(source_modules - names)
    assert not missing, f"missing from the wheel: {missing}"
    assert "super8/resources/super8.png" in names
