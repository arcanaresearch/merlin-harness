# pyinstaller spec for the macOS ARM onedir release

from PyInstaller.utils.hooks import collect_all, copy_metadata

packages = (
    "pg0",
    "playwright",
    "merlin_agents",
    "merlin_assessments",
    "merlin_browser",
    "merlin_config",
    "merlin_contracts",
    "merlin_findings",
    "merlin_kernel",
    "merlin_mcp",
    "merlin_memory",
    "merlin_packs",
    "merlin_permissions",
    "merlin_promotion",
    "merlin_providers",
    "merlin_readiness",
    "merlin_skills",
    "merlin_storage_postgres",
    "merlin_tools",
    "merlin_tui",
)
distributions = (
    "pg0-embedded",
    "playwright",
    "merlin-agents",
    "merlin-assessments",
    "merlin-browser",
    "merlin-config",
    "merlin-contracts",
    "merlin-findings",
    "merlin-kernel",
    "merlin-mcp",
    "merlin-memory",
    "merlin-packs",
    "merlin-permissions",
    "merlin-promotion",
    "merlin-providers",
    "merlin-readiness",
    "merlin-skills",
    "merlin-storage-postgres",
    "merlin-tools",
    "merlin-tui",
)

datas = []
binaries = []
# Frozen runtimes must bundle the idna codec: socket.getaddrinfo looks it up
# lazily and crashes with "LookupError: unknown encoding: idna" otherwise.
hiddenimports = ["encodings.idna"]
for package in packages:
    package_datas, package_binaries, package_imports = collect_all(package)
    if package == "merlin_packs":
        package_datas = [
            item for item in package_datas if not item[0].endswith(".rpk")
        ]
    datas += package_datas
    binaries += package_binaries
    hiddenimports += package_imports
for distribution in distributions:
    datas += copy_metadata(distribution)

analysis = Analysis(
    ["../scripts/frozen_entry.py"],
    pathex=[".."],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    noarchive=False,
)
pyz = PYZ(analysis.pure)
executable = EXE(
    pyz,
    analysis.scripts,
    [],
    exclude_binaries=True,
    name="merlin-runtime",
    console=True,
)
collection = COLLECT(
    executable,
    analysis.binaries,
    analysis.datas,
    strip=False,
    upx=False,
    name="merlin-runtime",
)
