"""Keep the catalog README aligned with the root README."""

from pathlib import Path


def test_catalog_readme_matches_root():
    root = Path(__file__).resolve().parents[1]
    source = (root / "README.md").read_text(encoding="utf-8")
    expected = source.replace("](docs/", "](../docs/").replace("](LICENSE)", "](../LICENSE)")
    assert (root / "optmem" / "README.md").read_text(encoding="utf-8") == expected


def test_readme_documents_catalog_install_only_when_listed():
    root = Path(__file__).resolve().parents[1]
    source = (root / "README.md").read_text(encoding="utf-8")
    assert "If `optmem-hermes` is listed in your Hermes catalog" in source
    assert "hermes plugins install optmem-hermes" in source
    assert "If it is not listed in your catalog, install a published release" in source
    assert 'pip install "git+https://github.com/rarf/optmem-hermes-plugin@v' in source
    assert "not yet in the Hermes catalog" not in source

