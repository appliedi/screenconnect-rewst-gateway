import os

import uvicorn


def main() -> None:
    uvicorn.run(
        "gateway.main:create_app_from_env",
        factory=True,
        host=os.environ.get("HOST", "0.0.0.0"),
        port=int(os.environ.get("PORT", "8080")),
        proxy_headers=True,
        ssl_certfile=os.environ.get("TLS_CERT_FILE") or None,
        ssl_keyfile=os.environ.get("TLS_KEY_FILE") or None,
        log_config=None,
    )


if __name__ == "__main__":
    main()
