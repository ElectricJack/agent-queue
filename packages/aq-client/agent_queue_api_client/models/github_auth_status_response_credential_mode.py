from enum import Enum


class GithubAuthStatusResponseCredentialMode(str, Enum):
    APP = "app"
    EXISTING_LOGIN = "existing_login"

    def __str__(self) -> str:
        return str(self.value)
