"""从菜品画像构建不含健康结论、营养和步骤正文的 RAG 文档。"""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict

from food_agent_v2.b1.consumer_views import RecipeRetrievalBuildView


class RagBuildDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")

    build_id: UUID
    source_manifest_hash: str
    recipe_id: int
    document_id: str
    name: str
    label_tags: tuple[str, ...]
    meal_tags: tuple[str, ...]
    population_tags: tuple[str, ...]
    dish_type_tags: tuple[str, ...]
    taste_tags: tuple[str, ...]
    cuisine_tags: tuple[str, ...]
    cooking_method_tags: tuple[str, ...]
    texture_tags: tuple[str, ...]
    scenario_tags: tuple[str, ...]
    ingredient_names: tuple[str, ...]
    ingredient_ids: tuple[int, ...]
    catalog_eligibility: str
    source_row_sha256: str
    searchable_text: str


def build_rag_documents_from_views(
    views: tuple[RecipeRetrievalBuildView, ...],
) -> tuple[list[RagBuildDocument], dict]:
    documents = []
    for view in views:
        searchable_text = " ".join(
            item
            for item in (
                view.name,
                " ".join(view.ingredient_display_names),
                " ".join(view.label_tags),
                " ".join(view.meal_tags),
                " ".join(view.population_tags),
                " ".join(view.dish_type_tags),
                " ".join(view.taste_tags),
                " ".join(view.cuisine_tags),
                " ".join(view.cooking_method_tags),
                " ".join(view.texture_tags),
                " ".join(view.scenario_tags),
            )
            if item
        )
        documents.append(
            RagBuildDocument(
                build_id=view.build_id,
                source_manifest_hash=view.source_manifest_hash,
                recipe_id=view.recipe_id,
                document_id=f"recipe_{view.recipe_id:04d}",
                name=view.name,
                label_tags=view.label_tags,
                meal_tags=view.meal_tags,
                population_tags=view.population_tags,
                dish_type_tags=view.dish_type_tags,
                taste_tags=view.taste_tags,
                cuisine_tags=view.cuisine_tags,
                cooking_method_tags=view.cooking_method_tags,
                texture_tags=view.texture_tags,
                scenario_tags=view.scenario_tags,
                ingredient_names=view.ingredient_display_names,
                ingredient_ids=view.ingredient_ids,
                catalog_eligibility=view.catalog_eligibility,
                source_row_sha256=view.source_row_sha256,
                searchable_text=searchable_text,
            )
        )
    return documents, {
        "stage": "rag_documents_from_views",
        "total_documents": len(documents),
        "status": "passed",
    }
