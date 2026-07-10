"""Back-compat shim: the sandbox worker now lives in the SDK so containers and
subprocesses share one entrypoint (``catlico_plugin_sdk._worker``)."""
from catlico_plugin_sdk._worker import main

if __name__ == "__main__":
    main()
