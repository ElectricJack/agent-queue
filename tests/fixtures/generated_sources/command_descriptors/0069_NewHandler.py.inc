class NewHandler:
    async def _cmd_newly_discovered(self, args):
        return {"success": True, "forwarded": args}

    async def _cmd_argumentless(self, args):
        return {"success": True}


