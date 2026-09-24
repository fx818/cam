"""OpenAPI spec for the CAM mock REST API fixture."""

from __future__ import annotations

from typing import Any

_JSON = "application/json"

OPENAPI_SPEC: dict[str, Any] = {
    "openapi": "3.0.4",
    "info": {
        "title": "CAM Mock REST API",
        "version": "1.0.0",
        "description": "Small authenticated JSON API used by CAM tests.",
    },
    "servers": [{"url": "/"}],
    "paths": {
        "/items": {
            "get": {
                "operationId": "listItems",
                "summary": "List items",
                "description": "Return items, optionally filtered by q.",
                "parameters": [
                    {
                        "name": "q",
                        "in": "query",
                        "required": False,
                        "schema": {"type": "string"},
                        "description": "Filter substring.",
                    },
                    {
                        "name": "limit",
                        "in": "query",
                        "required": False,
                        "schema": {"type": "integer"},
                        "description": "Maximum rows.",
                    },
                ],
                "responses": {
                    "200": {
                        "description": "Items",
                        "content": {
                            _JSON: {
                                "schema": {
                                    "type": "array",
                                    "items": {"type": "object"},
                                }
                            }
                        },
                    }
                },
            },
            "post": {
                "operationId": "createItem",
                "summary": "Create an item",
                "requestBody": {
                    "required": True,
                    "content": {
                        _JSON: {
                            "schema": {
                                "type": "object",
                                "required": ["name"],
                                "properties": {
                                    "name": {"type": "string", "description": "Item name."},
                                    "tags": {
                                        "type": "array",
                                        "items": {"type": "string"},
                                        "description": "Tags.",
                                    },
                                },
                            }
                        }
                    },
                },
                "responses": {"201": {"description": "Created"}},
            },
        },
        "/items/{item_id}": {
            "get": {
                "operationId": "getItem",
                "summary": "Get one item",
                "parameters": [
                    {
                        "name": "item_id",
                        "in": "path",
                        "required": True,
                        "schema": {"type": "string"},
                    }
                ],
                "responses": {
                    "200": {"description": "Item"},
                    "404": {"description": "Missing"},
                },
            },
        },
        "/whoami": {
            "get": {
                "operationId": "whoami",
                "summary": "Describe the caller's auth method",
                "responses": {"200": {"description": "Identity"}},
            },
        },
        "/plain": {
            "get": {
                "operationId": "plainText",
                "summary": "Plain text greeting",
                "responses": {
                    "200": {
                        "description": "Text",
                        "content": {"text/plain": {"schema": {"type": "string"}}},
                    }
                },
            },
        },
        "/missing": {
            "get": {
                "operationId": "missing",
                "summary": "Always 404",
                "responses": {"404": {"description": "Not found"}},
            },
        },
        "/boom": {
            "get": {
                "operationId": "boom",
                "summary": "Always 500",
                "responses": {"500": {"description": "Server error"}},
            },
        },
        "/throttled": {
            "get": {
                "operationId": "throttled",
                "summary": "Always 429",
                "responses": {"429": {"description": "Rate limited"}},
            },
        },
        "/slow": {
            "get": {
                "operationId": "slow",
                "summary": "Sleep before answering",
                "parameters": [
                    {
                        "name": "seconds",
                        "in": "query",
                        "required": False,
                        "schema": {"type": "number"},
                    }
                ],
                "responses": {"200": {"description": "Done"}},
            },
        },
        "/redirect": {
            "get": {
                "operationId": "redirect",
                "summary": "302 to /items",
                "responses": {"302": {"description": "Redirect"}},
            },
        },
        "/open": {
            "get": {
                "operationId": "openEndpoint",
                "summary": "Unauthenticated ping",
                "responses": {"200": {"description": "Pong"}},
            },
        },
    },
}
