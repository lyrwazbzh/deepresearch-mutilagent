"""Training utilities with optional heavyweight ML dependencies."""

__all__ = ["DeepResearchDatasetV2", "create_data_collator"]


def __getattr__(name):
    if name in __all__:
        from .data_loader_v2 import DeepResearchDatasetV2, create_data_collator
        return {
            "DeepResearchDatasetV2": DeepResearchDatasetV2,
            "create_data_collator": create_data_collator,
        }[name]
    raise AttributeError(name)
