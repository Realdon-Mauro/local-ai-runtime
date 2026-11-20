from lar.evaluation.catalog import ModelCatalog
from lar.evaluation.discovery import ModelInventoryEntry


def make_model(
    name: str = "test-model:latest",
    digest: str = "abc123",
) -> ModelInventoryEntry:
    return ModelInventoryEntry(
        name=name,
        digest=digest,
        size_bytes=1024,
        modified_at="2026-11-16T10:00:00Z",
        format="gguf",
        family="test",
        parameter_size="7B",
        quantization_level="Q4_K_M",
        capabilities=["completion", "tools"],
        model_info={"general.architecture": "test"},
    )


def test_save_scan_and_read_latest_models(tmp_path):
    catalog = ModelCatalog(tmp_path / "catalog.db")

    scan_id = catalog.save_scan([make_model()])
    models = catalog.list_latest_models()

    assert scan_id == 1
    assert len(models) == 1
    assert models[0]["name"] == "test-model:latest"
    assert models[0]["digest"] == "abc123"
    assert models[0]["metadata"]["capabilities"] == [
        "completion",
        "tools",
    ]


def test_scan_history_is_preserved(tmp_path):
    catalog = ModelCatalog(tmp_path / "catalog.db")

    first_id = catalog.save_scan([make_model()])
    second_id = catalog.save_scan(
        [make_model(digest="different-digest")]
    )

    scans = catalog.list_scans()
    first_models = catalog.get_scan_models(first_id)
    latest_models = catalog.list_latest_models()

    assert first_id != second_id
    assert len(scans) == 2
    assert first_models[0]["digest"] == "abc123"
    assert latest_models[0]["digest"] == "different-digest"


def test_empty_scan_is_supported(tmp_path):
    catalog = ModelCatalog(tmp_path / "catalog.db")

    scan_id = catalog.save_scan([])

    assert catalog.get_scan_models(scan_id) == []
    assert catalog.list_latest_models() == []
    assert catalog.list_scans()[0]["model_count"] == 0


def test_inspection_error_is_preserved(tmp_path):
    catalog = ModelCatalog(tmp_path / "catalog.db")
    model = make_model()
    model.inspection_error = "Metadata unavailable"

    catalog.save_scan([model])
    stored = catalog.list_latest_models()

    assert stored[0]["inspection_error"] == "Metadata unavailable"
