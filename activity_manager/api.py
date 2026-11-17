from urllib.parse import urlsplit

import httpx
from activity_info_api_client import AuthenticatedClient, errors
from activity_info_api_client.api.default import (
    get_database_tree,
    get_form_schema,
    get_form_tree,
    query_rows,
    get_user_databases,
    update_form_schema,
)
from activity_info_api_client.models.add_update_form_response import AddUpdateFormResponse
from activity_info_api_client.models.database import Database
from activity_info_api_client.models.database_resource_type import DatabaseResourceType
from activity_info_api_client.models.form_schema import FormSchema
from activity_info_api_client.models.form_tree import FormTree
from activity_info_api_client.models.query_rows_request import QueryRowsRequest
from activity_info_api_client.models.query_rows_response_200_item import QueryRowsResponse200Item

from .schema_names import schemas_from_tree
from .schema_identity import prepare_schema_for_write

DEFAULT_BASE_URL = 'https://www.activityinfo.org/resources'


class ActivityInfoService:
    """Session-scoped adapter around one reusable authenticated API client."""

    def __init__(self, base_url: str, token: str):
        base_url, token = base_url.strip(), token.strip()
        url = urlsplit(base_url)
        if (url.scheme not in ('http', 'https') or not url.hostname or url.username
                or url.password or url.query or url.fragment):
            raise ValueError('Enter a valid HTTP(S) API base URL.')
        if not token:
            raise ValueError('Enter an API token.')
        self.client = AuthenticatedClient(
            base_url=base_url.rstrip('/'), token=token,
            timeout=httpx.Timeout(30), raise_on_unexpected_status=True,
        )
        self._http_client = self.client.get_async_httpx_client()

    async def _request(self, endpoint, *, action='load data', **kwargs):
        if self._http_client.is_closed:
            raise ValueError('The connection is closed. Connect again.')
        try:
            response = await endpoint.asyncio_detailed(client=self.client, **kwargs)
        except errors.UnexpectedStatus as exc:
            response_text = exc.content.decode(errors='replace').strip()
            detail = f' Server response: {response_text[:500]}' if response_text else ''
            raise ValueError(
                f'Could not {action} (HTTP {exc.status_code}).{detail}'
            ) from exc
        except httpx.HTTPError as exc:
            raise ValueError(
                f'Could not {action}. Network error: {type(exc).__name__}: {exc}'
            ) from exc
        except (KeyError, TypeError, ValueError) as exc:
            detail = str(exc).strip() or repr(exc)
            raise ValueError(
                f'The server returned an invalid API response while trying to {action}. '
                f'{type(exc).__name__}: {detail}'
            ) from exc
        if response.status_code == 401:
            raise ValueError('Authentication failed. Check your API token.')
        return response.parsed

    async def get_databases(self) -> dict[str, str]:
        """Return database IDs mapped to sorted display names."""
        databases = await self._request(get_user_databases, action='load databases')
        if not isinstance(databases, list):
            raise ValueError('The server returned an invalid database response.')
        return {
            database.database_id: f'{database.label} ({database.database_id})'
            for database in sorted(
                databases,
                key=lambda database: (database.label.casefold(), database.database_id),
            )
        }

    async def get_forms(self, database_id: str) -> dict[str, str]:
        names = await self.get_form_names(database_id)
        return {identity: f'{name} ({identity})' for identity, name in sorted(
            names.items(), key=lambda item: (item[1].casefold(), item[0]))}

    async def get_form_names(self, database_id: str) -> dict[str, str]:
        """Include forms and subforms from all folders in the flat database tree."""
        database = await self._request(
            get_database_tree,
            action=f'load forms for database {database_id!r}',
            database_id=database_id,
        )
        if not isinstance(database, Database):
            raise ValueError('The server returned an invalid database response.')
        forms = [
            resource for resource in database.resources
            if resource.type_ in (DatabaseResourceType.FORM, DatabaseResourceType.SUB_FORM)
        ]
        return {
            resource.id: str(resource.label or resource.id)
            for resource in sorted(
                forms,
                key=lambda resource: (str(resource.label or resource.id).casefold(), resource.id),
            )
        }

    async def get_form_schema(self, form_id: str) -> dict:
        """Return a form schema as JSON-ready data."""
        schema = await self._request(
            get_form_schema,
            action=f'load form schema {form_id!r}',
            form_id=form_id,
        )
        if not isinstance(schema, FormSchema):
            raise ValueError('The server returned an invalid form schema response.')
        return schema.to_dict()

    async def get_form_tree(self, form_id: str) -> dict:
        tree = await self._request(
            get_form_tree, form_id=form_id, action='load related form schemas',
        )
        if not isinstance(tree, FormTree):
            raise ValueError('The server returned an invalid form tree response.')
        schemas = schemas_from_tree(tree.to_dict())
        if form_id not in schemas:
            raise ValueError('The form tree did not contain the selected form schema.')
        return schemas

    async def get_database_names(self) -> dict[str, str]:
        databases = await self._request(get_user_databases, action='load database names')
        if not isinstance(databases, list):
            raise ValueError('The server returned an invalid database response.')
        return {database.database_id: database.label for database in databases}

    async def query_rows(self, query: dict) -> list[dict]:
        """Evaluate column formulas through the generated POST query endpoint."""
        try:
            body = QueryRowsRequest.from_dict(query)
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError('The row query is invalid.') from exc
        rows = await self._request(
            query_rows, body=body, action='query rows',
        )
        if not isinstance(rows, list) or any(not isinstance(row, QueryRowsResponse200Item) for row in rows):
            raise ValueError('The server returned an invalid query rows response.')
        return [row.to_dict() for row in rows]

    async def put_form_schema(self, form_id: str, schema: dict) -> None:
        """Write a complete form schema using the generated API endpoint."""
        await self.put_resolved_form_schema(form_id, await prepare_schema_for_write(self, schema))

    async def put_resolved_form_schema(self, form_id: str, schema: dict) -> None:
        """Write the reviewed materialized target without repeating name lookups."""
        try:
            body = FormSchema.from_dict(schema)
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError('The target form schema is invalid.') from exc
        response = await self._request(
            update_form_schema,
            action='update the form schema',
            form_id=form_id,
            body=body,
        )
        if not isinstance(response, AddUpdateFormResponse):
            raise ValueError('The server returned an invalid form schema update response.')

    async def aclose(self) -> None:
        if not self._http_client.is_closed:
            await self._http_client.aclose()
