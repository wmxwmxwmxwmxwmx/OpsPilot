import json

from jsonschema import Draft202012Validator

from app.mcp.client import MCPFailure
from app.mcp.schemas import CATALOG
from app.tools.registry import EmptyInput, Tool
from app.workflow.privacy import CheckpointPrivacy


class MCPTool(Tool[EmptyInput]):
    safety = 'read_only'
    input_schema = EmptyInput

    def __init__(self, client, definition):
        if definition.name not in CATALOG:
            raise MCPFailure('mcp_tool_denied')
        schema = definition.input_schema
        if (len(json.dumps(schema)) > 2048 or schema.get('type') != 'object'
                or schema.get('properties', {}) or schema.get('required', [])
                or set(schema) - {'type', 'properties', 'required', 'additionalProperties', 'title'}):
            raise MCPFailure('mcp_schema_not_reviewed')
        Draft202012Validator.check_schema(schema)
        self.validator = Draft202012Validator(schema)
        self.client, self.remote_name = client, definition.name
        local = CATALOG[definition.name]()
        self.name = 'mcp_' + definition.name
        self.description = '通过受控本地 MCP 服务调用：' + local.description
        self.checkpoint_fields = local.checkpoint_fields
        self.local_policy = local

    async def execute(self, arguments):
        arguments = self.input_schema.model_validate(arguments)
        self.validator.validate(arguments.model_dump())
        raw = await self.client.call(self.remote_name, arguments.model_dump())
        # Reuse reviewed numeric projections; no remote text/descriptions in state.
        return CheckpointPrivacy().result(self.local_policy, raw)
