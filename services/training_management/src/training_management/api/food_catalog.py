"""HU014 (ADR-037): `GET /v1/food-catalog` con `ETag`.

Catálogo cerrado y versionado de alimentos para cualquier actor autenticado. El `ETag` es
`"<catalog_version>"`; un `If-None-Match` coincidente responde `304` sin cuerpo.
"""

from http import HTTPStatus
from typing import Annotated

from fastapi import APIRouter, Header, Response
from pydantic import BaseModel

from training_management.api.identity import AUTH_RESPONSES, TokenVerifier, authenticate
from training_management.application.queries.food_catalog import GetFoodCatalog
from training_management.domain.nutrition.catalog import FoodCatalog, FoodCategory, FoodUnit


class FoodCatalogItemResponse(BaseModel):
    food_code: str
    label_key: str
    category: FoodCategory
    default_unit: FoodUnit
    allowed_units: list[FoodUnit]
    allergens: list[str]
    incompatible_with: list[str]


class FoodCatalogResponse(BaseModel):
    catalog_version: str
    items: list[FoodCatalogItemResponse]

    @classmethod
    def from_catalog(cls, catalog: FoodCatalog) -> "FoodCatalogResponse":
        return cls(
            catalog_version=catalog.catalog_version,
            items=[
                FoodCatalogItemResponse(
                    food_code=item.food_code,
                    label_key=item.label_key,
                    category=item.category,
                    default_unit=item.default_unit,
                    allowed_units=list(item.allowed_units),
                    allergens=list(item.allergens),
                    incompatible_with=list(item.incompatible_with),
                )
                for item in catalog.items
            ],
        )


def etag_matches(if_none_match: str | None, etag: str) -> bool:
    if not if_none_match:
        return False
    candidates = {item.strip().removeprefix("W/") for item in if_none_match.split(",")}
    return "*" in candidates or etag in candidates


def build_food_catalog_router(
    *, verify_token: TokenVerifier, get_catalog: GetFoodCatalog
) -> APIRouter:
    router = APIRouter(tags=["food-catalog"])

    @router.get(
        "/v1/food-catalog",
        response_model=FoodCatalogResponse,
        summary="Catálogo de alimentos (HU014, ADR-037)",
        responses={
            **AUTH_RESPONSES,
            HTTPStatus.OK.value: {
                "headers": {
                    "ETag": {
                        "description": '`"<catalog_version>"`.',
                        "schema": {"type": "string"},
                    }
                }
            },
            HTTPStatus.NOT_MODIFIED.value: {
                "description": "`If-None-Match` coincide con el `ETag` vigente; sin cuerpo.",
                "headers": {
                    "ETag": {
                        "description": "Versión vigente del catálogo, entre comillas.",
                        "schema": {"type": "string"},
                    }
                },
            },
        },
    )
    def food_catalog(
        response: Response,
        if_none_match: Annotated[str | None, Header(alias="If-None-Match")] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> Response | FoodCatalogResponse:
        caller = authenticate(verify_token, authorization)
        if isinstance(caller, Response):
            return caller
        current = get_catalog(caller)
        if etag_matches(if_none_match, current.etag):
            return Response(status_code=HTTPStatus.NOT_MODIFIED, headers={"ETag": current.etag})
        response.headers["ETag"] = current.etag
        return FoodCatalogResponse.from_catalog(current)

    return router
