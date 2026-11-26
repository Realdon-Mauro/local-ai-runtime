from dataclasses import asdict, dataclass, field
from typing import Any

from lar.providers.ollama import OllamaError, OllamaProvider


@dataclass
class ModelInventoryEntry:
    name: str
    digest: str | None = None
    size_bytes: int | None = None
    modified_at: str | None = None

    format: str | None = None
    family: str | None = None
    families: list[str] = field(default_factory=list)
    parameter_size: str | None = None
    quantization_level: str | None = None

    capabilities: list[str] = field(default_factory=list)
    model_info: dict[str, Any] = field(default_factory=dict)

    inspection_error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


async def discover_models(
    provider: OllamaProvider,
) -> list[ModelInventoryEntry]:
    """Scopre i modelli installati e ne raccoglie i metadati."""

    installed_models = await provider.list_models()
    inventory: list[ModelInventoryEntry] = []

    for installed in installed_models:
        name = installed["name"]
        details = installed.get("details") or {}

        entry = ModelInventoryEntry(
            name=name,
            digest=installed.get("digest"),
            size_bytes=(
                installed["size"]
                if isinstance(installed.get("size"), int)
                and not isinstance(installed.get("size"), bool)
                and installed["size"] > 0
                else None
            ),
            modified_at=installed.get("modified_at"),
            format=details.get("format"),
            family=details.get("family"),
            families=details.get("families") or [],
            parameter_size=details.get("parameter_size"),
            quantization_level=details.get("quantization_level"),
        )

        try:
            metadata = await provider.show_model(name)

            entry.capabilities = metadata.get("capabilities") or []
            entry.model_info = metadata.get("model_info") or {}

            show_details = metadata.get("details") or {}

            entry.format = entry.format or show_details.get("format")
            entry.family = entry.family or show_details.get("family")
            entry.families = entry.families or show_details.get("families") or []
            entry.parameter_size = (
                entry.parameter_size or show_details.get("parameter_size")
            )
            entry.quantization_level = (
                entry.quantization_level
                or show_details.get("quantization_level")
            )

        except OllamaError as exc:
            entry.inspection_error = str(exc)

        inventory.append(entry)

    return inventory
