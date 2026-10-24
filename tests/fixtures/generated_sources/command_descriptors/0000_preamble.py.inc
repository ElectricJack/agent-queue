"""Descriptor coverage, compatibility and disabled generated execution."""
from __future__ import annotations

import hashlib
import json
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from src.commands.command_inventory import command_snapshot
from src.commands.contracts import CONTRACTS
from src.commands.contracts.builtin import set_handler_provider
from src.commands.contracts.generated import (
    GeneratedInvocationDisabled,
    JsonObjectResult,
    argument_model,
    generated_registration,
)
from src.commands.contracts.models import canonical_execution_document
from src.commands.contracts.registry import ContractRegistry
from src.commands.descriptors import (
    PLAYBOOK_DENIALS,
    HandlerCommand,
    PluginCommand,
    argument_usage,
    build_descriptor_snapshot,
    discover_handler_inventory,
)
from src.commands.handler import CommandHandler
from src.config import AppConfig
from src.mcp_registration import _discover_all_commands, effective_tool_definitions
from src.plugins.base import InternalPlugin
from src.plugins.registry import PluginRegistry
from src.tools.command_catalogue import render_command_catalogue

