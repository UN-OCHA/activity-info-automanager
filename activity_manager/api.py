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
    update_database,
)
from activity_info_api_client.models.add_update_form_response import AddUpdateFormResponse
from activity_info_api_client.models.database import Database
from activity_info_api_client.models.database_resource_type import DatabaseResourceType
from activity_info_api_client.models.form_schema import FormSchema
from activity_info_api_client.models.update_database_request import UpdateDatabaseRequest
from activity_info_api_client.models.form_tree import FormTree
from activity_info_api_client.models.query_rows_request import QueryRowsRequest
from activity_info_api_client.models.query_rows_response_200_item import QueryRowsResponse200Item

from .schema_names import schemas_from_tree
from .schema_identity import prepare_schema_for_write
from .endpoint_evidence import EndpointFailure, EndpointReceipt, FailureKind, Observation, WriteEvidence
from .resource_state import (
    DatabaseResourcesPayload, Existence, FormPayload, LoadStatus, ResourceKey, ResourceState,
)

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

    async def _endpoint_evidence(self, endpoint, model, **kwargs):
        """Use generated encoding/parsing, retaining the HTTP response before parsing.

        The generated detailed helper parses before returning and loses response
        evidence on malformed JSON. Keep this small boundary against client 2.0.4.
        Legacy UI requests continue using _request until the executor cutover.
        """
        if self._http_client.is_closed:
            return None, EndpointFailure(FailureKind.UNAVAILABLE, 'The connection is closed.')
        try:
            response = await self._http_client.request(**endpoint._get_kwargs(**kwargs))
        except httpx.HTTPError as exc:
            return None, EndpointFailure(FailureKind.UNAVAILABLE, f'{type(exc).__name__}: {exc}')
        receipt = EndpointReceipt(response.status_code, dict(response.headers), response.text)
        if response.status_code != 200:
            kind = (FailureKind.MISSING if response.status_code in (404, 410) else
                    FailureKind.FORBIDDEN if response.status_code in (401, 403) else
                    FailureKind.UNAVAILABLE if response.status_code >= 500 else FailureKind.REJECTED)
            return None, EndpointFailure(kind, f'HTTP {response.status_code}', receipt)
        try:
            parsed = endpoint._parse_response(client=self.client, response=response)
            if not isinstance(parsed, model):
                raise ValueError('Unexpected response model.')
            data = parsed.to_dict()
            version = (data.get('database', {}).get('version') if model is AddUpdateFormResponse else
                       data.get('schemaVersion') if model is FormSchema else data.get('version'))
            return EndpointReceipt(response.status_code, dict(response.headers), response.text,
                                   data, version), None
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            return None, EndpointFailure(FailureKind.INVALID, f'{type(exc).__name__}: {exc}', receipt)

    async def observe_resource(self, key):
        schema = key.kind.value == 'form_schema'
        receipt, failure = await self._endpoint_evidence(
            get_form_schema if schema else get_database_tree, FormSchema if schema else Database,
            **({'form_id': key.identity} if schema else {'database_id': key.identity}))
        if failure is not None:
            existence = Existence.ABSENT if failure.kind == FailureKind.MISSING else Existence.UNKNOWN
            return Observation(key, existence, receipt=failure.receipt, failure=failure)
        from .immutable import thaw
        try:
            data = thaw(receipt.body)
            if not schema and data['databaseId'] != key.identity:
                raise ValueError('Database response identity differs from the requested identity.')
            resources = data.get('resources', [])
            if not schema:
                for entry in resources:
                    if entry.get('type') in ('FORM', 'FOLDER') and not entry.get('parentId'):
                        entry['parentId'] = key.identity
            if not schema and len({entry['id'] for entry in resources}) != len(resources):
                raise ValueError('Duplicate database resource identities.')
            payload = FormPayload(data) if schema else DatabaseResourcesPayload(
                {entry['id']: entry for entry in resources})
            state = ResourceState(key, payload, load_status=LoadStatus.LOADED, server_version=receipt.version)
            return Observation(key, Existence.PRESENT, state, receipt)
        except (KeyError, TypeError, ValueError) as exc:
            return Observation(key, Existence.UNKNOWN, receipt=receipt,
                               failure=EndpointFailure(FailureKind.INVALID, str(exc), receipt))

    async def write_schema_evidence(self, form_id, schema):
        """Write an already reviewed schema; no resolution or ID allocation here."""
        try:
            body = FormSchema.from_dict(schema)
            if body.id != form_id:
                raise ValueError('Request and schema identities disagree.')
        except (KeyError, TypeError, ValueError) as exc:
            return WriteEvidence(failure=EndpointFailure(FailureKind.INVALID, str(exc)))
        receipt, failure = await self._endpoint_evidence(
            update_form_schema, AddUpdateFormResponse, form_id=form_id, body=body)
        return WriteEvidence(receipt if failure is None else failure.receipt, failure)

    async def get_database_resources(self, database_id):
        state = await self.get_database_state(database_id)
        return {entry['id']: entry for entry in state.payload.to_list()}

    async def get_database_state(self, database_id):
        observed = await self.observe_resource(ResourceKey.database(database_id))
        if observed.failure or observed.state is None:
            raise ValueError(observed.failure.message if observed.failure else 'Database unavailable.')
        return observed.state

    async def write_resources_evidence(self, database_id, payload):
        try:
            body = UpdateDatabaseRequest.from_dict(payload)
        except (KeyError, TypeError, ValueError) as exc:
            return WriteEvidence(failure=EndpointFailure(FailureKind.INVALID, str(exc)))
        receipt, failure = await self._endpoint_evidence(update_database, Database, database_id=database_id, body=body)
        return WriteEvidence(receipt if failure is None else failure.receipt, failure)
