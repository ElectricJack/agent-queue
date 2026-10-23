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

ROOT = Path(__file__).resolve().parents[1]
GOLDEN = ROOT / "tests/fixtures/contracts/legacy-command-descriptors.json"
C2_CHANGES = ROOT / "tests/fixtures/contracts/observation-contract-changes.json"
SCRIPT_CHANGES = ROOT / "tests/fixtures/contracts/script-contract-changes.json"
MAIN_CHANGES = ROOT / "tests/fixtures/contracts/main-refresh-contract-changes.json"
DEV_CHANGES = ROOT / "tests/fixtures/contracts/dev-refresh-contract-changes.json"
WITHHELD_CHANGES = ROOT / "tests/fixtures/contracts/withheld-source-contract-changes.json"
EPIC_CHANGES = ROOT / "tests/fixtures/contracts/epic-integration-contract-changes.json"
EPIC_COMMANDS = ("integration_cutover_containment", "integration_reconcile_git_truth")
WATCH_CHANGES = ROOT / "tests/fixtures/contracts/watch-contract-changes.json"
INGEST_CHANGES = ROOT / "tests/fixtures/contracts/ingestion-replay-contract-changes.json"
HIBERNATION_CHANGES = ROOT / "tests/fixtures/contracts/hibernation-contract-changes.json"
DISPATCH_CHANGES = ROOT / "tests/fixtures/contracts/dispatch-wait-contract-changes.json"
BATCH_FIX_CHANGES = ROOT / "tests/fixtures/contracts/batch-fix-contract-changes.json"
MERGE_CHANGES = ROOT / "tests/fixtures/contracts/epic-main-refresh-contract-changes.json"
LEAF_CHANGES = ROOT / "tests/fixtures/contracts/batch745-leaf-contract-changes.json"
COMPLETION_CHANGES = ROOT / "tests/fixtures/contracts/fast-smooth-completion-contract-changes.json"
PUBLICATION_CHANGES = ROOT / "tests/fixtures/contracts/publication-delegation-contract-changes.json"
BATCH_REPAIR_CHANGES = ROOT / "tests/fixtures/contracts/batch-repair-contract-changes.json"
DEV_HIBERNATION_CHANGES = ROOT / "tests/fixtures/contracts/hibernation-main-refresh-contract-changes.json"
SUPERVISOR_HEALTH_CHANGES = ROOT / "tests/fixtures/contracts/supervisor-health-contract-changes.json"
TARGET_REPAIR_CHANGES = ROOT / "tests/fixtures/contracts/target-repair-contract-changes.json"
PR_REPAIR_CHANGES = ROOT / "tests/fixtures/contracts/pr-repair-publication-contract-changes.json"
        if path in failover:
            assert failover[path]["previous_sha256"] == digest
            historical = ROOT / failover[path]["preserved_previous_path"]
            assert hashlib.sha256(historical.read_bytes()).hexdigest() == digest
            assert failover[path]["sha256"] != digest
            digest = failover[path]["sha256"]
