"""The simplified bot retires only its six historical slash commands."""

from src.discord.slash_commands import RETIRED_SLASH_COMMANDS, unregister_retired_commands


class FakeTree:
    def __init__(self):
        self.commands = {
            name: object()
            for name in (*RETIRED_SLASH_COMMANDS, "plugin-command", "unrelated")
        }

    def remove_command(self, name):
        return self.commands.pop(name, None)


def test_unregister_removes_exactly_the_six_retired_names():
    tree = FakeTree()

    removed = unregister_retired_commands(tree)

    assert set(removed) == RETIRED_SLASH_COMMANDS
    assert set(tree.commands) == {"plugin-command", "unrelated"}


def test_unregister_is_idempotent():
    tree = FakeTree()
    unregister_retired_commands(tree)
    assert unregister_retired_commands(tree) == ()
