from pathlib import Path
import zipfile

ROOT = Path(__file__).resolve().parents[2]
OUTPUT = ROOT / "worker/public/downloads/notebook-studio-helper.zip"
FILES = [Path("README.md"), Path(".env.example"), Path("pyproject.toml"), Path("run.sh")]
FILES += sorted(path.relative_to(ROOT) for path in (ROOT / "notebook_studio").rglob("*.py"))
FILES += [Path("notebook_studio/web/index.html")]
FILES += sorted(path.relative_to(ROOT) for path in (ROOT / "notebook_studio/web/static").glob("*"))

OUTPUT.parent.mkdir(parents=True, exist_ok=True)
with zipfile.ZipFile(OUTPUT, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
    for relative in FILES:
        source = ROOT / relative
        if source.is_file():
            archive.write(source, Path("notebook-studio-helper") / relative)

with zipfile.ZipFile(OUTPUT) as archive:
    names = archive.namelist()
    if any(name.endswith("/.env") or ".sqlite3" in name or "/.venv/" in name or "/.dev.vars" in name for name in names):
        raise SystemExit("Refusing to package local credentials or database files.")
    if not any(name.endswith("/.env.example") for name in names):
        raise SystemExit("The helper archive is missing .env.example.")
print(f"Packaged {len(names)} safe files to {OUTPUT} ({OUTPUT.stat().st_size} bytes).")
