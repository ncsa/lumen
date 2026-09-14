"""Shared model-resolution logic for canonical names and aliases.

A requested model name may be either a canonical ``model_configs.model_name``
or an alias (a ``model_aliases`` row) pointing at a canonical model. Every
request path resolves once through :func:`resolve_model_config` so authorization,
consent, quota, endpoint selection, billing, and metrics all use the canonical
identity; only the client-facing ``model`` response field keeps the requested
name (see the API routes).
"""

from sqlalchemy import select

from ..extensions import db
from ..models.model_alias import ModelAlias
from ..models.model_config import ModelConfig


def resolve_model_config(name: str):
    """Return the canonical active :class:`ModelConfig` for ``name``, or ``None``.

    ``name`` may be a canonical model name or an alias. A target that is
    disabled or past its end date is never returned, so an alias never falls
    back to a retired model.
    """
    if not name:
        return None
    config = db.session.execute(
        select(ModelConfig).where(ModelConfig.model_name == name, ModelConfig.active)
    ).scalar_one_or_none()
    if config is not None:
        return config
    return db.session.execute(
        select(ModelConfig)
        .join(ModelAlias, ModelAlias.model_config_id == ModelConfig.id)
        .where(ModelAlias.alias == name, ModelConfig.active)
    ).scalar_one_or_none()
