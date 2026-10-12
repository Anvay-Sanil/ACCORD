from functools import lru_cache
from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    database_url: str
    app_origin: str = "https://localhost:8443"
    public_deployment: bool = False
    session_secure: bool = True
    paypal_base_url: str = "https://api-m.sandbox.paypal.com"
    paypal_client_id: str = ""
    paypal_client_secret: str = ""
    paypal_merchant_id: str = ""
    paypal_webhook_id: str = ""
    fault_drop_capture_response: bool = False
    openai_api_key: str = ""
    openai_model: str = "gpt-4.1-2025-04-14"

    @model_validator(mode="after")
    def safe_deployment(self):
        if self.paypal_base_url != "https://api-m.sandbox.paypal.com":
            raise ValueError("This build only permits PayPal Sandbox")
        if self.public_deployment and (not self.session_secure or
                                      not self.app_origin.startswith("https://") or
                                      self.fault_drop_capture_response):
            raise ValueError("Public deployment requires HTTPS, secure cookies, and fault injection OFF")
        if self.app_origin.endswith("/"):
            raise ValueError("APP_ORIGIN must be an origin without a trailing slash")
        return self


@lru_cache
def settings():
    return Settings()

