"""REST API adapter for the Quantro trading engine."""


def create_app(*args, **kwargs):
    from .app import create_app as _create_app

    return _create_app(*args, **kwargs)


def __getattr__(name: str):
    if name == "app":
        from .app import app

        return app
    raise AttributeError(name)


__all__ = ["app", "create_app"]
