"""Mock adapter for preserved command fixtures; production uses ActivityInfoService."""
from activity_manager.confirmed_plan import AttemptBinding, ConfirmationAttempt
from activity_manager.executor import Executor
from activity_manager.endpoint_evidence import Observation, WriteEvidence
from activity_manager.resource_state import ResourceKind, ResourceState, FormPayload, DatabaseResourcesPayload, LoadStatus, Existence
from activity_manager.form_folders import form_location
from unittest.mock import AsyncMock


def locations(execution):
    result = {}
    for resource in execution.display_state.resources:
        if resource.key.kind == ResourceKind.DATABASE_RESOURCES:
            for identity, entry in resource.state.payload.entries.items():
                if entry.get('type') == 'FORM':
                    result[identity] = form_location(resource.key.identity, resource.state.payload.entries, identity)
    return result


def install(service):
    async def observe(key):
        payload = (FormPayload(await service.get_form_schema(key.identity)) if key.kind == ResourceKind.FORM_SCHEMA
                   else DatabaseResourcesPayload(await service.get_database_resources(key.identity)))
        return Observation(key, Existence.PRESENT, ResourceState(key, payload, load_status=LoadStatus.LOADED))
    async def write_schema(identity, payload):
        await service.put_form_schema(identity, payload)
        return WriteEvidence()
    async def write_resources(database, payload):
        for patch in payload['resourceUpdates']:
            await service.move_form(database, patch['id'], patch['parentId'])
        return WriteEvidence()
    service.observe_resource = observe
    service.write_schema_evidence = write_schema
    service.write_resources_evidence = write_resources


async def execute(manager, service, choice='rollback_all'):
    install(service)
    candidate = manager.candidate_plan
    binding = AttemptBinding(str(manager._evaluation_version), {}, 'test')
    attempt = ConfirmationAttempt(candidate, binding, related_schemas=manager.related_schemas)
    attempt.confirm(attempt.plan.attempt_id, binding, candidate)
    return await Executor(service).execute(attempt, binding, candidate, AsyncMock(return_value=choice))
