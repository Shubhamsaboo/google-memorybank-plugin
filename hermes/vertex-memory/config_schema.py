"""Vertex Memory Bank's declared config surface — rendered by the generic
Hermes admin-dashboard panel (loaded by path, must import only the pure-data
schema module; never the provider package itself)."""

from plugins.memory.config_schema import (
    KIND_NUMBER, KIND_SELECT, KIND_TEXT, ProviderConfigSchema, ProviderField, ProviderFieldOption,
)

CONFIG_SCHEMA = ProviderConfigSchema(
    name="vertex-memory",
    label="Vertex AI Memory Bank",
    docs_url="https://github.com/Shubhamsaboo/google-memorybank-plugin/tree/main/hermes/vertex-memory",
    fields=(
        ProviderField(
            key="project_id", label="GCP project ID", kind=KIND_TEXT,
            description="Google Cloud project hosting the Vertex Agent Engine reasoning engine.",
            placeholder="my-gcp-project", inline=True,
        ),
        ProviderField(
            key="location", label="Region", kind=KIND_TEXT, default="us-central1",
            description="GCP region of the reasoning engine (e.g. us-central1).",
            inline=True,
        ),
        ProviderField(
            key="reasoning_engine_id", label="Reasoning engine ID", kind=KIND_TEXT,
            description="Vertex Agent Engine reasoning engine id. Create one with create_engine.py (see README).",
            placeholder="1234567890", inline=True,
        ),
        ProviderField(
            key="scope_key", label="Scope dimension", kind=KIND_SELECT, default="user_id",
            description="What memory isolation is keyed on. user_id shares memory across every agent for the same user.",
            options=(
                ProviderFieldOption("user_id", "User ID", "Cross-agent memory for the same user"),
                ProviderFieldOption("agent_name", "Agent name", "Per-agent memory, shared across users"),
            ),
            inline=True,
        ),
        ProviderField(
            key="scope_value", label="Static scope value", kind=KIND_TEXT, default="hermes-user",
            description="Fallback scope value for CLI/single-user sessions without a gateway user_id.",
        ),
        ProviderField(
            key="top_k", label="Recall top-K", kind=KIND_NUMBER, default="10",
            description="Max memories returned per recall.",
        ),
        ProviderField(
            key="max_distance", label="Max relevance distance", kind=KIND_NUMBER,
            description="Relevance cutoff for recall (lower = stricter). Blank = no cutoff.",
            placeholder="(no cutoff)",
        ),
    ),
)
