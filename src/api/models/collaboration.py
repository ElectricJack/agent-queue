"""Concrete collaboration request and response models shared with public contracts."""

from src.commands.contracts.collaboration import (
    CollaborationAcceptArgs,
    CollaborationCloseArgs,
    CollaborationCloseValue,
    CollaborationCreateArgs,
    CollaborationGetArgs,
    CollaborationGetValue,
    CollaborationListArgs,
    CollaborationListValue,
    CollaborationValue,
)


class CollaborationResponse(CollaborationValue):
    success: bool = True


class CollaborationGetResponse(CollaborationGetValue):
    success: bool = True


class CollaborationListResponse(CollaborationListValue):
    success: bool = True


class CollaborationCloseResponse(CollaborationCloseValue):
    success: bool = True


REQUEST_MODELS = {
    "collaboration_create": CollaborationCreateArgs,
    "collaboration_accept": CollaborationAcceptArgs,
    "collaboration_get": CollaborationGetArgs,
    "collaboration_list": CollaborationListArgs,
    "collaboration_close": CollaborationCloseArgs,
}
RESPONSE_MODELS = {
    "collaboration_create": CollaborationResponse,
    "collaboration_accept": CollaborationResponse,
    "collaboration_get": CollaborationGetResponse,
    "collaboration_list": CollaborationListResponse,
    "collaboration_close": CollaborationCloseResponse,
}
