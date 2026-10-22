"""Owner-scoped cancellation through the shared transactional decision owner."""


async def cancel(service, args, *, principal, scope):
    return await service.decisions.cancel(args, principal=principal, scope=scope, handler=None)
