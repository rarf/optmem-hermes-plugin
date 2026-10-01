"""Keep the catalog README aligned with the root README."""

from pathlib import Path


def test_catalog_readme_matches_root():
    root = Path(__file__).resolve().parents[1]
    source = (root / "README.md").read_text(encoding="utf-8")
    expected = source.replace("](docs/", "](../docs/").replace("](LICENSE)", "](../LICENSE)")
    assert (root / "optmem" / "README.md").read_text(encoding="utf-8") == expected

